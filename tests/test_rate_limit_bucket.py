"""Tests for the SQLite-backed token-bucket rate limiter (1.6.0)."""
from __future__ import annotations

import threading
import time

import pytest

from skr_crypto.server.rate_limit_bucket import (
    BucketParams,
    TokenBucketLimiter,
)


class TestBucketParams:
    def test_capacity_must_be_positive(self):
        with pytest.raises(ValueError):
            BucketParams(capacity=0, refill_per_sec=1.0)

    def test_refill_must_be_positive(self):
        with pytest.raises(ValueError):
            BucketParams(capacity=10, refill_per_sec=0)


class TestTokenBucketInMemory:
    def _limiter(self) -> TokenBucketLimiter:
        return TokenBucketLimiter(db_path=None)

    def test_first_n_pass_then_blocked(self):
        """Capacity=5 means first 5 pass, 6th hits empty bucket."""
        lim = self._limiter()
        params = BucketParams(capacity=5, refill_per_sec=0.001)  # essentially no refill
        results = [lim.check("ip", "1.1.1.1", params) for _ in range(7)]
        assert results == [True, True, True, True, True, False, False]

    def test_keys_are_independent(self):
        """Different keys have independent buckets."""
        lim = self._limiter()
        params = BucketParams(capacity=2, refill_per_sec=0.001)
        # Drain key A
        assert lim.check("ip", "A", params) is True
        assert lim.check("ip", "A", params) is True
        assert lim.check("ip", "A", params) is False
        # Key B is fresh
        assert lim.check("ip", "B", params) is True

    def test_key_types_are_independent(self):
        """ip and token are different namespaces."""
        lim = self._limiter()
        params = BucketParams(capacity=1, refill_per_sec=0.001)
        assert lim.check("ip", "X", params) is True
        assert lim.check("ip", "X", params) is False
        # Same value under different key_type still has its own bucket
        assert lim.check("token", "X", params) is True

    def test_refill_replenishes_over_time(self):
        """After waiting for refill, the bucket has tokens again."""
        lim = self._limiter()
        params = BucketParams(capacity=1, refill_per_sec=10.0)
        assert lim.check("ip", "Y", params) is True
        assert lim.check("ip", "Y", params) is False
        # 0.2s elapsed → refilled 2 tokens (capped to capacity=1)
        time.sleep(0.2)
        assert lim.check("ip", "Y", params) is True

    @pytest.mark.invariant
    def test_concurrent_check_does_not_overshoot(self):
        """INVARIANT: under N concurrent threads on a capacity-K bucket,
        AT MOST K threads receive True. If this regresses, the limiter
        is silently letting bursts past the configured ceiling."""
        lim = self._limiter()
        K = 5
        N = 50
        params = BucketParams(capacity=K, refill_per_sec=0.0001)
        results: list[bool] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(N)

        def worker():
            barrier.wait()
            r = lim.check("ip", "concurrent", params)
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        passes = sum(1 for r in results if r)
        assert passes <= K, (
            f"bucket K={K} let {passes} concurrent threads through; "
            f"the cap was breached"
        )


class TestSweeper:
    def test_idle_bucket_swept(self):
        lim = TokenBucketLimiter(db_path=None)
        params = BucketParams(capacity=10, refill_per_sec=1.0)
        # Drain one bucket
        lim.check("ip", "stale-ip", params)
        # Manually backdate updated_at
        rec = lim._memory[("ip", "stale-ip")]
        rec["updated_at"] = time.time() - 7200  # 2 hours ago
        # Sweep with 1-hour threshold
        n = lim.sweep_idle(idle_threshold_sec=3600)
        assert n == 1
        assert ("ip", "stale-ip") not in lim._memory

    def test_active_bucket_not_swept(self):
        lim = TokenBucketLimiter(db_path=None)
        params = BucketParams(capacity=10, refill_per_sec=1.0)
        lim.check("ip", "fresh-ip", params)
        # Sweep should NOT remove a freshly-touched bucket
        n = lim.sweep_idle(idle_threshold_sec=3600)
        assert n == 0
        assert ("ip", "fresh-ip") in lim._memory


class TestPersistence:
    def test_state_persists_across_restart(self, tmp_path):
        """A drained bucket in run #1 is still drained in run #2."""
        db = tmp_path / "rl.db"
        lim = TokenBucketLimiter(db_path=str(db))
        params = BucketParams(capacity=2, refill_per_sec=0.0001)
        # Drain
        assert lim.check("ip", "persistent", params) is True
        assert lim.check("ip", "persistent", params) is True
        assert lim.check("ip", "persistent", params) is False
        lim.close()

        lim2 = TokenBucketLimiter(db_path=str(db))
        # Bucket was empty when we closed; it should still be empty
        # (modulo a tiny refill from the time we slept), so the next
        # check is false.
        assert lim2.check("ip", "persistent", params) is False
        lim2.close()
