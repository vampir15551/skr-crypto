"""Tests for cost-control features: pre-flight energy estimate, dynamic
fee_limit, MAX_ENERGY_BURN_TRX guard, resource visibility on /balance and
/health."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

VALID_ADDRESS = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"


class TestComputeFeeLimit:
    """TronClient.compute_fee_limit_sun() picks the right fee_limit based on
    the estimate, clamped to the configured bounds."""

    def test_no_estimate_returns_ceiling(self, mock_tron):
        from skr_crypto.server.config import USDT_FEE_LIMIT_SUN
        from skr_crypto.server.tron_client import TronClient
        c = TronClient()
        c._energy_price_sun = 420
        assert c.compute_fee_limit_sun(None) == USDT_FEE_LIMIT_SUN

    def test_small_estimate_clamped_to_min_floor(self, mock_tron):
        from skr_crypto.server.tron_client import _MIN_FEE_LIMIT_SUN, TronClient
        c = TronClient()
        c._energy_price_sun = 420
        # 1000 energy * 420 sun/u * 1.3 = 546000 sun, floor is 5_000_000.
        fee = c.compute_fee_limit_sun(1000)
        assert fee == _MIN_FEE_LIMIT_SUN

    def test_typical_warm_estimate_scales_with_price(self, mock_tron):
        from skr_crypto.server.config import FEE_LIMIT_SAFETY_MULT, USDT_FEE_LIMIT_SUN
        from skr_crypto.server.tron_client import TronClient
        c = TronClient()
        c._energy_price_sun = 420
        # Warm receiver: ~13000 energy. expect within [5M, 30M] sun.
        fee = c.compute_fee_limit_sun(13000)
        expected = int(Decimal(13000) * Decimal(420) * FEE_LIMIT_SAFETY_MULT)
        assert 5_000_000 <= fee <= USDT_FEE_LIMIT_SUN
        assert fee == expected

    def test_huge_estimate_capped_at_ceiling(self, mock_tron):
        from skr_crypto.server.config import USDT_FEE_LIMIT_SUN
        from skr_crypto.server.tron_client import TronClient
        c = TronClient()
        c._energy_price_sun = 420
        # 10M energy * 420 * 1.3 >> 30M sun — must be clamped.
        assert c.compute_fee_limit_sun(10_000_000) == USDT_FEE_LIMIT_SUN


class TestMaxEnergyBurnGuard:
    """If the on-chain estimate exceeds MAX_ENERGY_BURN_TRX, the request must
    be rejected with 400 ENERGY_TOO_EXPENSIVE — the operator should stake
    TRX or rent energy instead of burning."""

    def test_expensive_estimate_rejected(self, client, auth_headers, mock_tron, monkeypatch):
        from skr_crypto.server import routes as routes_mod

        # 100_000 energy × 420 sun = 42 TRX burn, above default 20 TRX ceiling.
        mock_tron.estimate_transfer_energy = MagicMock(return_value=100_000)
        mock_tron.energy_price_sun = MagicMock(return_value=420)
        monkeypatch.setattr(routes_mod, "MAX_ENERGY_BURN_TRX", Decimal("20"))

        resp = client.post("/api/v1/send", json={
            "to_address": VALID_ADDRESS,
            "amount": "10",
            "idempotency_key": "cost-test-1",
        }, headers=auth_headers)

        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "ENERGY_TOO_EXPENSIVE"
        assert body["estimated_energy"] == 100_000
        # Never broadcasts
        assert mock_tron.send_usdt.call_count == 0

    def test_cheap_estimate_passes(self, client, auth_headers, mock_tron):
        # 13000 × 420 = 5.46 TRX — below 20 TRX ceiling.
        mock_tron.estimate_transfer_energy = MagicMock(return_value=13000)
        mock_tron.energy_price_sun = MagicMock(return_value=420)
        mock_tron.compute_fee_limit_sun = MagicMock(return_value=7_000_000)

        resp = client.post("/api/v1/send", json={
            "to_address": VALID_ADDRESS,
            "amount": "10",
            "idempotency_key": "cost-test-2",
        }, headers=auth_headers)

        assert resp.status_code == 200
        # Broadcast called WITH dynamic fee_limit from compute_fee_limit_sun
        assert mock_tron.send_usdt.call_count == 1
        kwargs = mock_tron.send_usdt.call_args.kwargs
        assert kwargs.get("fee_limit_sun") == 7_000_000

    def test_zero_max_burn_disables_guard(self, client, auth_headers, mock_tron, monkeypatch):
        """MAX_ENERGY_BURN_TRX=0 must disable the guard entirely."""
        from skr_crypto.server import routes as routes_mod
        monkeypatch.setattr(routes_mod, "MAX_ENERGY_BURN_TRX", Decimal("0"))

        mock_tron.estimate_transfer_energy = MagicMock(return_value=1_000_000)
        mock_tron.energy_price_sun = MagicMock(return_value=420)

        resp = client.post("/api/v1/send", json={
            "to_address": VALID_ADDRESS,
            "amount": "10",
            "idempotency_key": "cost-test-3",
        }, headers=auth_headers)
        assert resp.status_code == 200


class TestResourceVisibility:
    """/balance and /health should expose the operator's energy / bandwidth /
    staking state so they can see when they're about to start burning TRX."""

    def test_balance_includes_resources(self, client, auth_headers, mock_tron):
        mock_tron.get_resource_summary = MagicMock(return_value={
            "energy_available": 50_000,
            "energy_limit": 100_000,
            "bandwidth_free_available": 600,
            "bandwidth_paid_available": 2000,
            "tron_power": 15000,
        })
        resp = client.get("/api/v1/balance", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["energy_available"] == 50_000
        assert data["bandwidth_free_available"] == 600
        assert data["bandwidth_paid_available"] == 2000
        assert data["tron_power_staked"] == 15000

    def test_health_includes_resources_when_connected(self, client, auth_headers, mock_tron):
        mock_tron.check_connection = MagicMock(return_value=True)
        mock_tron.get_resource_summary = MagicMock(return_value={
            "energy_available": 7500,
            "energy_limit": 10000,
            "bandwidth_free_available": 300,
            "bandwidth_paid_available": 0,
            "tron_power": 1000,
        })
        resp = client.get("/api/v1/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_connected"] is True
        assert data["energy_available"] == 7500
        assert data["tron_power_staked"] == 1000

    def test_health_skips_resource_call_when_disconnected(
        self, client, auth_headers, mock_tron,
    ):
        """No point querying resources when the node is unreachable."""
        mock_tron.check_connection = MagicMock(return_value=False)
        mock_tron.get_resource_summary = MagicMock(side_effect=Exception("must not call"))
        resp = client.get("/api/v1/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_connected"] is False
        assert data["energy_available"] == 0
        assert mock_tron.get_resource_summary.call_count == 0


class TestHealthLive:
    """/health/live must be unauthenticated and cheap (no TRON RPC)."""

    def test_live_no_auth(self, client):
        resp = client.get("/api/v1/health/live")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data

    def test_live_does_not_call_tron(self, client, mock_tron):
        mock_tron.check_connection = MagicMock(side_effect=Exception("must not call"))
        mock_tron.get_resource_summary = MagicMock(side_effect=Exception("must not call"))
        resp = client.get("/api/v1/health/live")
        assert resp.status_code == 200

    def test_live_bypasses_rate_limit(self, client, monkeypatch):
        """Aggressive k8s liveness polling must not trigger a 429."""
        from skr_crypto.server.server import _limiter
        # Exhaust the limiter for this IP.
        for _ in range(200):
            _limiter.check("testclient")
        resp = client.get("/api/v1/health/live")
        assert resp.status_code == 200


class TestEstimateTransferEnergy:
    """The estimate helper wraps get_estimated_energy and falls back
    gracefully when the node refuses to estimate."""

    def test_success_returns_int(self, mock_tron):
        from skr_crypto.server.tron_client import TronClient
        c = TronClient()
        c.client = mock_tron.client
        c.address = mock_tron.address
        c.client.get_estimated_energy = MagicMock(return_value=13500)

        energy = c.estimate_transfer_energy(VALID_ADDRESS, Decimal("1.5"))
        assert energy == 13500

    def test_zero_returned_is_treated_as_unavailable(self, mock_tron):
        from skr_crypto.server.tron_client import TronClient
        c = TronClient()
        c.client = mock_tron.client
        c.address = mock_tron.address
        c.client.get_estimated_energy = MagicMock(return_value=0)
        assert c.estimate_transfer_energy(VALID_ADDRESS, Decimal("1")) is None

    def test_exception_is_swallowed(self, mock_tron):
        from skr_crypto.server.tron_client import TronClient
        c = TronClient()
        c.client = mock_tron.client
        c.address = mock_tron.address
        c.client.get_estimated_energy = MagicMock(side_effect=Exception("no estimate"))
        # Must not propagate — estimate is advisory, we fall back to the cap.
        assert c.estimate_transfer_energy(VALID_ADDRESS, Decimal("1")) is None
