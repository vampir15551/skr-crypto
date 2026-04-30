# Monitoring & metrics

The service exposes Prometheus metrics at `GET /api/v1/metrics` (authenticated) and writes structured logs to stdout. This page is the catalogue.

Implementation: `skr_crypto/server/metrics.py` and `skr_crypto/server/logging_config.py`.

---

## Metric catalogue

All metrics are namespaced under `skr_crypto_`. Histograms have suffix `_seconds` (durations) or `_sun` / `_trx` (amounts).

### Counters

| Metric | Labels | Notes |
|---|---|---|
| `skr_crypto_tx_broadcast_total` | `result=success\|failed` | Every `wallet.send_usdt` attempt, success or failure |
| `skr_crypto_tx_rejected_total` | `reason=…` | Pre-broadcast rejections — see reasons table below |
| `skr_crypto_tx_duplicate_total` | — | Idempotency-cache hits |
| `skr_crypto_idempotency_conflicts_total` | — | 409 IDEMPOTENCY_CONFLICT |
| `skr_crypto_idempotency_unresolved_total` | — | 409 IDEMPOTENCY_UNRESOLVED |
| `skr_crypto_rate_limit_drops_total` | — | 429s the middleware emitted |
| `skr_crypto_http_requests_total` | `method, path, status=2xx\|3xx\|4xx\|5xx` | Every request, status class only |

**`tx_rejected_total{reason=…}` values:**

| Reason | When |
|---|---|
| `invalid_address` | `to_address` failed base58check |
| `insufficient_usdt` | wallet's USDT < amount |
| `insufficient_trx` | wallet's TRX < `MIN_TRX_RESERVE` |
| `energy_too_expensive` | estimated burn > `MAX_ENERGY_BURN_TRX` |
| `risk_too_high` | recipient verdict ≥ `RISK_BLOCK_LEVEL` |
| `rpc_failed` | preflight TronGrid RPC raised |

### Histograms

| Metric | Buckets | Notes |
|---|---|---|
| `skr_crypto_tx_duration_seconds` | exponential, ~0.1 → 60 s | End-to-end /send latency; `result` label |
| `skr_crypto_energy_estimated` | log-spaced 1k → 200k | Per-tx energy estimate |
| `skr_crypto_energy_burn_trx_estimated` | log-spaced 0.1 → 30 | Per-tx estimated TRX burn |
| `skr_crypto_fee_limit_sun` | log-spaced 5_000_000 → 30_000_000 | Per-tx fee_limit picked |

### Gauges

| Metric | Notes |
|---|---|
| `skr_crypto_uptime_seconds` | Fresh on every `/metrics` scrape |
| `skr_crypto_node_connected` | 1 if last `check_connection` succeeded |
| `skr_crypto_idempotency_store_size` | Total rows in idempotency DB |
| `skr_crypto_trx_balance` | Last balance reading; refreshed by /balance + /send |
| `skr_crypto_usdt_balance` | Same |
| `skr_crypto_energy_available` | From `/balance` resource snapshot |
| `skr_crypto_bandwidth_free_available` | Same |
| `skr_crypto_bandwidth_paid_available` | Same |
| `skr_crypto_tron_power_staked` | Same |

**Important nuance:** balance gauges are refreshed only when the relevant code path runs. They are NOT polled by the `/metrics` scrape itself — that would burn TronGrid quota for every Prometheus pull. To get live balances in your dashboard, either:

- Have your Prometheus job hit `/api/v1/balance` periodically (small `prometheus.yml` rule), or
- Trust that organic `/send` traffic refreshes the gauges, or
- Scrape `/api/v1/wallets` and parse JSON in a Grafana JSON datasource.

---

## Recommended Prometheus jobs

```yaml
scrape_configs:
  - job_name: skr-crypto
    scrape_interval: 30s
    static_configs:
      - targets: ['skr-crypto.internal:8000']
    metrics_path: /api/v1/metrics
    authorization:
      type: ApiKey
      credentials_file: /etc/prometheus/skr-crypto.token
```

`scrape_interval: 30s` is plenty — none of the metrics change faster than that in normal operation. Aggressive scraping wastes CPU on nothing.

---

## Recommended alerts

```yaml
groups:
  - name: skr-crypto
    rules:
      # Hard fail — service is broken
      - alert: skrCryptoNodeDown
        expr: skr_crypto_node_connected == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "skr-crypto cannot reach TronGrid for >5m"

      - alert: skrCryptoAuditFailures
        expr: increase(skr_crypto_http_requests_total{status="5xx"}[10m]) > 0
        labels:
          severity: critical
        annotations:
          summary: "skr-crypto returned 5xx — investigate audit / RPC"

      # Treasury health
      - alert: skrCryptoUsdtLow
        expr: skr_crypto_usdt_balance < 1000
        for: 30m
        labels:
          severity: warning
        annotations:
          summary: "USDT balance below 1k for 30m"

      - alert: skrCryptoTrxLow
        expr: skr_crypto_trx_balance < 100
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "TRX reserve low — refill before next /send"

      # Operational
      - alert: skrCryptoRateLimited
        expr: rate(skr_crypto_rate_limit_drops_total[5m]) > 1
        labels:
          severity: warning
        annotations:
          summary: "Rate limit dropping >1/s — caller misbehaving"

      - alert: skrCryptoIdempotencyUnresolved
        expr: increase(skr_crypto_idempotency_unresolved_total[1h]) > 0
        labels:
          severity: critical
        annotations:
          summary: "UNRESOLVED idempotency state — manual reconcile required"
```

---

## Logs

The service writes to stdout in two modes:

- **Human text** (default for local dev): `LOG_FORMAT=text`. Colour codes if a TTY, plain otherwise.
- **JSON** (default for containers): `LOG_FORMAT=json`. One record per line, parseable by any log aggregator.

JSON record shape:

```json
{
  "timestamp": "2026-04-30T07:42:13.123456+00:00",
  "level": "INFO",
  "logger": "payouts",
  "message": "[SEND] SUCCESS | txid=abc... wallet=cold T... -> T... amount=245.50 USDT risk=low elapsed=1.43s",
  "request_id": "a1b2c3d4e5f6"
}
```

`request_id` is propagated from the middleware's `X-Request-ID` header — every line emitted while handling a request gets the same id, so you can `grep request_id=a1b2…` to reconstruct a single request's timeline.

### Useful log greps

```bash
# Every successful /send
grep '\[SEND\] SUCCESS' app.log | wc -l

# Every risk block
grep '\[ERROR\] RISK_TOO_HIGH' app.log

# Duplicates (high count = caller bug)
grep '\[SEND\] DUPLICATE' app.log | wc -l

# Slow /send (>5s)
grep '\[SEND\] SUCCESS' app.log | grep -oE 'elapsed=[0-9.]+s' | awk -F= '$2 > 5'

# Multi-wallet auto-pick decisions
grep 'auto-pick winner' app.log

# Boot-time wallet load
grep 'WalletPool initialised' app.log
```

---

## Sentinel logs you should always see

These should appear on every healthy boot. Their absence is a signal.

```text
[INFO] payouts: TronClient initialised (network: mainnet)
[INFO] payouts: Key provider: encrypted_file
[INFO] payouts: WalletPool initialised with 2 wallet(s): cold=TColdxxx..., hot=THotxxx...
[INFO] payouts.audit: Durable audit file opened: /var/lib/skr-crypto/data/audit.log
[INFO] payouts: [SANCTIONS] loaded N TRX entries from <url>
[INFO] payouts: [STARTUP_CHECK] Done — window_start=... checked=N ...
[INFO] payouts: Payout service ready
```

**Specifically alarming:**

- `WalletPool initialised with zero wallets` — config error, no wallet was loaded.
- `Idempotency store: IN-MEMORY` warning when `IDEMPOTENCY_DB_PATH` should be set in production.
- `Sanctions list load failed` followed by `not loaded` in `/api/v1/risk` reports — the OFAC check is silently `skip`ping.
- `[STARTUP_CHECK] DUPLICATE recipient` — same address received >1 transfer in MSK day. Investigate.

---

## Grafana dashboard suggestions

Three panels that earn their screen real estate:

### 1. /send funnel
```promql
sum by (result) (rate(skr_crypto_tx_broadcast_total[5m]))
```
Stacked area; success on top.

### 2. Reject reasons over time
```promql
sum by (reason) (increase(skr_crypto_tx_rejected_total[1h]))
```
Bar chart per reason. Reveals "every Tuesday at 14:00 we get a wave of `risk_too_high`" patterns.

### 3. Treasury balances
```promql
skr_crypto_usdt_balance
skr_crypto_trx_balance
```
Two-line graph. Threshold annotations at the `MIN_TRX_RESERVE` line.

---

## See also

- [HTTP API: /metrics](api-reference.md#get-apiv1metrics) — the wire format
- [Audit log](audit-log.md) — distinct from operational logs
- [Architecture](architecture.md) — what each subsystem actually does
