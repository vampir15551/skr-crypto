"""End-to-end rate-limit tests against /api/v1.

The 1.6.0 limiter is the SQLite-backed token bucket from
`rate_limit_bucket.py`. These tests exercise the IP-keyed gate via
the `_limiter` shim in `server.py` (which delegates to the bucket
when the lifespan has initialised it; in-memory limiter when not).
"""
from __future__ import annotations

from unittest.mock import patch


class TestRateLimit:
    def test_rate_limit_exceeded(self, client, auth_headers):
        """Hammering an endpoint should trigger 429 once the bucket
        is drained."""
        from skr_crypto.server import rate_limit_bucket as rl_mod
        from skr_crypto.server.rate_limit_bucket import (
            TokenBucketLimiter,
        )

        # Replace the module-level singleton with a tight in-memory
        # bucket: capacity=3, no meaningful refill.
        original = rl_mod.limiter
        rl_mod.limiter = TokenBucketLimiter(db_path=None)
        try:
            with patch(
                "skr_crypto.server.server.RATE_LIMIT_IP_CAPACITY", 3
            ), patch(
                "skr_crypto.server.server.RATE_LIMIT_IP_REFILL_PER_SEC", 0.001
            ):
                for _ in range(3):
                    resp = client.get("/api/v1/balance", headers=auth_headers)
                    assert resp.status_code == 200, resp.text
                resp = client.get("/api/v1/balance", headers=auth_headers)
                assert resp.status_code == 429
                assert resp.json()["code"] == "RATE_LIMIT_EXCEEDED"
        finally:
            rl_mod.limiter = original

    def test_health_bypasses_rate_limit(self, client, auth_headers):
        """Health endpoints are exempt from the limiter."""
        from skr_crypto.server import rate_limit_bucket as rl_mod
        from skr_crypto.server.rate_limit_bucket import TokenBucketLimiter

        original = rl_mod.limiter
        rl_mod.limiter = TokenBucketLimiter(db_path=None)
        try:
            with patch(
                "skr_crypto.server.server.RATE_LIMIT_IP_CAPACITY", 1
            ), patch(
                "skr_crypto.server.server.RATE_LIMIT_IP_REFILL_PER_SEC", 0.001
            ):
                # Drain the limiter on /balance
                client.get("/api/v1/balance", headers=auth_headers)
                resp = client.get("/api/v1/balance", headers=auth_headers)
                assert resp.status_code == 429
                # Health passes regardless
                resp = client.get("/api/v1/health", headers=auth_headers)
                assert resp.status_code == 200
        finally:
            rl_mod.limiter = original
