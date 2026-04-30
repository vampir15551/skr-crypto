"""Token-bucket rate limiter, persisted in SQLite. See ADR 0009.

Replaces the in-memory sliding-window limiter that shipped in 1.0-1.5.

Design:

  - Two key dimensions: ``key_type='ip'`` and ``key_type='token'``.
    A request must pass BOTH gates (per-IP first, then per-token).
  - State lives in the same SQLite database as idempotency + tokens
    (so backups + WAL semantics are unified). Keyed-and-keyed on
    ``(key_type, key_value)``.
  - Lazy refill: on each ``check()`` we compute current tokens as
    ``min(capacity, last_tokens + elapsed*refill)``.
  - Periodic sweeper deletes idle buckets to keep the table bounded.

Concurrency:

  - Uses ``BEGIN IMMEDIATE`` (write transaction) per check to ensure
    two threads can't both decrement the same bucket from 1 to 0
    simultaneously. SQLite's transactional update handles the race.

Cost: ~50-200 microseconds per check on typical hardware. Negligible compared
to /send's TronGrid round trips.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("payouts")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    key_type    TEXT NOT NULL,
    key_value   TEXT NOT NULL,
    tokens      REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (key_type, key_value)
);
CREATE INDEX IF NOT EXISTS idx_rl_updated ON rate_limit_buckets(updated_at);
"""


@dataclass(frozen=True)
class BucketParams:
    """Capacity = max burst (instantaneous). refill_per_sec = sustained
    ceiling. Effective steady-state ceiling is roughly capacity + refill
    over the operator's window of interest."""
    capacity: int
    refill_per_sec: float

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {self.capacity}")
        if self.refill_per_sec <= 0:
            raise ValueError(f"refill_per_sec must be > 0, got {self.refill_per_sec}")


class TokenBucketLimiter:
    """SQLite-backed token bucket. Two key dimensions (`ip` and `token`).

    Each ``check(key_type, key_value, params)`` either consumes one token
    from the bucket (returning ``True``) or returns ``False`` if the
    bucket is empty.

    For tests, ``db_path=None`` falls back to in-memory dict storage."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path
        self._lock = threading.RLock()
        self._memory: dict[tuple[str, str], dict] = {}
        if db_path:
            self._conn = sqlite3.connect(
                db_path, check_same_thread=False, timeout=30.0,
                isolation_level=None,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(_SCHEMA)
        else:
            self._conn = None

    def check(self, key_type: str, key_value: str, params: BucketParams) -> bool:
        """Return True if the request fits, False if rate-limited.

        On True, decrements one token from the bucket. On False, the
        bucket is unchanged (we still update last_seen timestamp via
        ``updated_at`` so the sweeper sees it as recent — that's
        intentional, a flooded caller stays in the table)."""
        now = time.time()
        if self._conn is not None:
            return self._check_sqlite(key_type, key_value, params, now)
        with self._lock:
            return self._check_memory(key_type, key_value, params, now)

    # ------ SQLite path ----------------------------------------------------

    def _check_sqlite(
        self, key_type: str, key_value: str, params: BucketParams, now: float,
    ) -> bool:
        # BEGIN IMMEDIATE acquires a RESERVED lock — two concurrent
        # threads can't both compute tokens from the same row.
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "SELECT tokens, updated_at FROM rate_limit_buckets "
                "WHERE key_type=? AND key_value=?",
                (key_type, key_value),
            )
            row = cur.fetchone()
            if row is None:
                # Fresh bucket starts at capacity; consume one.
                tokens = float(params.capacity) - 1
                self._conn.execute(
                    "INSERT OR REPLACE INTO rate_limit_buckets "
                    "(key_type, key_value, tokens, updated_at) VALUES (?, ?, ?, ?)",
                    (key_type, key_value, max(tokens, 0), now),
                )
                self._conn.execute("COMMIT")
                return tokens >= 0  # True since capacity >= 1
            current, last_updated = float(row[0]), float(row[1])
            elapsed = max(0.0, now - last_updated)
            current = min(float(params.capacity), current + elapsed * params.refill_per_sec)
            if current < 1.0:
                # Update timestamp only — bucket remains drained
                self._conn.execute(
                    "UPDATE rate_limit_buckets SET tokens=?, updated_at=? "
                    "WHERE key_type=? AND key_value=?",
                    (current, now, key_type, key_value),
                )
                self._conn.execute("COMMIT")
                return False
            current -= 1.0
            self._conn.execute(
                "UPDATE rate_limit_buckets SET tokens=?, updated_at=? "
                "WHERE key_type=? AND key_value=?",
                (current, now, key_type, key_value),
            )
            self._conn.execute("COMMIT")
            return True
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    # ------ memory path ----------------------------------------------------

    def _check_memory(
        self, key_type: str, key_value: str, params: BucketParams, now: float,
    ) -> bool:
        key = (key_type, key_value)
        rec = self._memory.get(key)
        if rec is None:
            self._memory[key] = {"tokens": float(params.capacity) - 1, "updated_at": now}
            return True
        elapsed = max(0.0, now - rec["updated_at"])
        rec["tokens"] = min(
            float(params.capacity),
            rec["tokens"] + elapsed * params.refill_per_sec,
        )
        if rec["tokens"] < 1.0:
            rec["updated_at"] = now
            return False
        rec["tokens"] -= 1.0
        rec["updated_at"] = now
        return True

    # ------ sweeper --------------------------------------------------------

    def sweep_idle(self, *, idle_threshold_sec: float) -> int:
        """Delete buckets whose ``updated_at`` is older than the threshold.

        Bounded: the threshold should be > capacity/refill so that a
        legitimate caller's bucket can recover before being deleted.
        Returns the number of rows removed."""
        cutoff = time.time() - idle_threshold_sec
        if self._conn is not None:
            cur = self._conn.execute(
                "DELETE FROM rate_limit_buckets WHERE updated_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0
        with self._lock:
            stale = [k for k, v in self._memory.items() if v["updated_at"] < cutoff]
            for k in stale:
                del self._memory[k]
            return len(stale)

    # ------ test helpers ---------------------------------------------------

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._memory.clear()
            if self._conn is not None:
                self._conn.execute("DELETE FROM rate_limit_buckets")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


# ---------------------------------------------------------------------------
# Periodic sweeper thread
# ---------------------------------------------------------------------------


class _SweeperThread(threading.Thread):
    """Daemon thread that runs ``limiter.sweep_idle`` on a fixed cadence."""

    def __init__(
        self,
        limiter: TokenBucketLimiter,
        *,
        interval_sec: float,
        idle_threshold_sec: float,
    ) -> None:
        super().__init__(name="rate-limit-sweeper", daemon=True)
        self._limiter = limiter
        self._interval = interval_sec
        self._idle = idle_threshold_sec
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                n = self._limiter.sweep_idle(idle_threshold_sec=self._idle)
                if n:
                    log.debug("[RATELIMIT] sweeper removed %d idle buckets", n)
            except Exception as exc:
                log.warning("[RATELIMIT] sweep failed: %s", exc)
            self._stop.wait(self._interval)


# ---------------------------------------------------------------------------
# Module-level singleton (initialised by lifespan)
# ---------------------------------------------------------------------------


limiter: TokenBucketLimiter | None = None
_sweeper: _SweeperThread | None = None


def init_limiter(
    db_path: str | None,
    *,
    sweep_interval_sec: float = 300.0,
) -> TokenBucketLimiter:
    """Construct + start the limiter + sweeper. Idempotent.

    The idle-threshold is set to ~10 capacity-windows so legitimate
    callers don't lose their bucket between bursts."""
    global limiter, _sweeper
    if _sweeper is not None:
        _sweeper.stop()
    if limiter is not None:
        try:
            limiter.close()
        except Exception:
            pass
    limiter = TokenBucketLimiter(db_path=db_path)
    # Idle threshold: 1 hour. Generous; keeps the table small without
    # punishing legitimate-but-quiet callers.
    _sweeper = _SweeperThread(
        limiter,
        interval_sec=sweep_interval_sec,
        idle_threshold_sec=3600.0,
    )
    _sweeper.start()
    log.info(
        "[RATELIMIT] token-bucket initialised (db=%s, sweeper interval=%.0fs)",
        db_path or "in-memory", sweep_interval_sec,
    )
    return limiter


def shutdown_limiter() -> None:
    global limiter, _sweeper
    if _sweeper is not None:
        _sweeper.stop()
        _sweeper = None
    if limiter is not None:
        try:
            limiter.close()
        except Exception:
            pass
        limiter = None
