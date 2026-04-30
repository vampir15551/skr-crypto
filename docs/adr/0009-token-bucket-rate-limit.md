# 0009 — Persisted token-bucket rate limiter

## Status

Accepted. 2026-04-30. Introduced in 1.5.0. Supersedes the in-memory
sliding-window limiter.

## Context

The 1.0–1.4 rate limiter (`server.server._RateLimiter`) is a sliding
window in process memory:

```python
class _RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> bool:
        ...
```

It does the job at small scale but has three concrete problems we've
hit:

- **State lost on restart.** Whatever sliding-window data the limiter
  collected vanishes. A misbehaving caller that just got rate-limited
  gets a clean slate after every deploy.
- **Memory growth on flat traffic.** Every IP that hits the service
  for the first time creates a list. Without a sweep, the dict grows
  forever (the per-key timestamp prune is bounded but the keys
  accumulate).
- **Burst-vs-steady semantics are bad.** Sliding window allows 30 req
  in second 0–1 and 30 more in second 59–60 — a 60-req burst across
  two adjacent windows. Token bucket spreads this out naturally.

ADR 0008 introduced per-caller API tokens. The natural follow-up is
"rate limit per token, not just per IP" — but the in-memory limiter
keys by IP only, and adding a second key dimension makes the memory
problem worse.

## Decision

Replace the sliding-window in-memory limiter with a **token-bucket
algorithm persisted in SQLite**, keyed by both IP and token ID.

Schema (in the same `idempotency.db` as the existing store):

```sql
CREATE TABLE rate_limit_buckets (
    key_type    TEXT NOT NULL,         -- 'ip' or 'token'
    key_value   TEXT NOT NULL,         -- the IP string or token ID
    tokens      REAL NOT NULL,         -- current token count, fractional
    updated_at  REAL NOT NULL,         -- unix timestamp of last refill
    PRIMARY KEY (key_type, key_value)
);
CREATE INDEX idx_rl_updated ON rate_limit_buckets(updated_at);
```

Algorithm per check:

```python
def check(key_type: str, key_value: str, *, capacity: int, refill_per_sec: float) -> bool:
    """Token bucket: returns True if the request fits, False if rate-limited."""
    with self._db.transaction():
        row = SELECT tokens, updated_at FROM rate_limit_buckets
              WHERE key_type=? AND key_value=?
        now = time.time()
        if row:
            elapsed = now - row.updated_at
            tokens = min(capacity, row.tokens + elapsed * refill_per_sec)
        else:
            tokens = capacity
        if tokens < 1:
            UPDATE ... SET tokens=?, updated_at=? -- record the failed attempt
            return False
        tokens -= 1
        UPSERT INTO rate_limit_buckets ...
        return True
```

Defaults (configurable via env):

| Key type | Capacity | Refill rate | Effective ceiling |
|---|---|---|---|
| IP | `RATE_LIMIT_IP_CAPACITY=30` | `RATE_LIMIT_IP_REFILL_PER_SEC=0.5` | 30 burst, 30/min sustained |
| Token | `RATE_LIMIT_TOKEN_CAPACITY=100` | `RATE_LIMIT_TOKEN_REFILL_PER_SEC=2.0` | 100 burst, 120/min sustained |

The IP bucket is the **first** gate; the token bucket is the **second**.
A request needs to pass both. This means a misbehaving authenticated
caller can't drown out other authenticated callers behind the same
NAT / proxy.

A periodic sweep (every 5 minutes via a background thread) deletes
buckets whose `updated_at` is older than the longest-window equivalent
plus a margin. This bounds DB size to O(active recent callers), not
O(all callers ever).

## Consequences

**Restart-stable rate limiting.** A caller that was rate-limited
before a restart is still rate-limited on the next request to the
same bucket. Operationally important — restarts shouldn't be a
"bypass rate limits" trick.

**Per-token quotas without dict growth.** Each token gets its own
bucket; sweep keeps the table bounded.

**Burst behaviour matches operator intuition.** A token with capacity
100 / refill 2/s allows a 100-burst followed by ~2 req/s sustained.
This is what most callers want for retry storms.

**~50 µs SQLite overhead per request.** Negligible compared to /send
(which already takes 1-3 seconds end-to-end including TronGrid). On
read-only endpoints (~5 ms each) this is ~1% overhead — acceptable.

**Race conditions are handled by SQLite's transaction isolation.**
Two concurrent threads both arriving at "tokens=1" can't both
decrement to 0; the transactional UPDATE ensures one wins.

**The metric `skr_crypto_rate_limit_drops_total` gains a `key_type`
label** (`ip` vs `token`). Operators can graph "which dimension is
firing more". Cardinality stays low (2 values).

**Configuration adds three env vars.** Defaults match the old
limiter's behaviour for IP-keyed checks, so a v1.4 → v1.5 deploy
without any config change preserves observed limits. Token-bucket
config is opt-in; absent token-scoped config means token-keyed
checks are bypassed (compat with v1.4 deploys without tokens).

**The `RATE_LIMIT_MAX` and `RATE_LIMIT_WINDOW` env vars are deprecated
but still honoured.** If set, they translate to:

```
RATE_LIMIT_IP_CAPACITY = RATE_LIMIT_MAX
RATE_LIMIT_IP_REFILL_PER_SEC = RATE_LIMIT_MAX / RATE_LIMIT_WINDOW
```

A loud deprecation warning is logged on boot.

## Alternatives considered

- **Redis as the rate-limit store.** Industry default. Rejected
  because adding Redis as a service dependency for a single feature
  is overkill — the project already has SQLite, the QPS is low, and
  Redis introduces an HA story we don't currently have.
- **Leaky bucket instead of token bucket.** Equivalent semantically;
  token bucket is more straightforward to compute lazily on read
  (which fits SQLite better than a continuous drain).
- **Stay with sliding window, just persist the timestamps.** Solves
  the restart problem but not the memory growth or the burst-edge
  semantics. Not worth the SQLite migration if we're not also
  fixing the other two.
- **Tier-based limits keyed by token scope.** "admin tokens get
  unlimited, send tokens get 30/min, read tokens get 600/min." Not
  in this ADR — capability is "scope-aware limiting" but defaults
  treat all tokens equally, scope-aware tuning is a future env-var
  if operators ask.
- **Per-endpoint sub-limits** ("/send is special, 1/sec; /balance
  is cheap, 60/min"). Considered, rejected for simplicity. The
  global send-lock already serialises /send; per-endpoint nuance
  doesn't add much.

## Related

- [ADR 0008](0008-per-caller-api-tokens.md) — token system this
  limiter builds on.
- [ADR 0007](0007-engineering-safety-practices.md) — auth + rate-limit
  changes are money-path-adjacent and follow that ADR's gates.
- `tests/test_rate_limit_bucket.py` — invariant tests.
