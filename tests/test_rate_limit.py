from __future__ import annotations

from unittest.mock import patch


class TestRateLimit:
    def test_rate_limit_exceeded(self, client, auth_headers):
        """Hammering the endpoint should trigger 429."""
        with patch("skr_crypto.server.server.RATE_LIMIT_MAX", 3), \
             patch("skr_crypto.server.server.RATE_LIMIT_WINDOW", 60):
            # Reset the limiter internal state
            from skr_crypto.server.server import _limiter
            _limiter._hits.clear()

            for i in range(3):
                resp = client.get("/api/v1/balance", headers=auth_headers)
                assert resp.status_code == 200

            resp = client.get("/api/v1/balance", headers=auth_headers)
            assert resp.status_code == 429
            assert resp.json()["code"] == "RATE_LIMIT_EXCEEDED"

    def test_health_bypasses_rate_limit(self, client, auth_headers):
        """Health endpoint should not be rate-limited."""
        with patch("skr_crypto.server.server.RATE_LIMIT_MAX", 1), \
             patch("skr_crypto.server.server.RATE_LIMIT_WINDOW", 60):
            from skr_crypto.server.server import _limiter
            _limiter._hits.clear()

            # Exhaust rate limit on balance
            client.get("/api/v1/balance", headers=auth_headers)
            resp = client.get("/api/v1/balance", headers=auth_headers)
            assert resp.status_code == 429

            # Health should still work
            resp = client.get("/api/v1/health", headers=auth_headers)
            assert resp.status_code == 200
