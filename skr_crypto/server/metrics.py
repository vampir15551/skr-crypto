"""Prometheus metrics.

A private CollectorRegistry is used so this module doesn't touch
prometheus_client's process-wide default. That keeps tests reload-safe
and avoids accidental double-registration if the package is vendored
somewhere else.

Counters record events. Gauges reflect last-known state (refreshed by
/balance, /health, and post-send hooks — never by the /metrics scrape
itself, which would mean an RPC call per Prometheus poll).

Histogram buckets are chosen against real TRC-20 cost ranges:
  - energy: 5k (cheap warm) / 13k (typical warm) / 32k (cold) / 100k+ (contract hot spot)
  - burn TRX: 0.1 → 50 (covers both rented/staked and full-burn regimes)
  - fee_limit: 5M → 30M sun (our floor → ceiling)
"""
from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Private registry — don't pollute the global one.
registry = CollectorRegistry()

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

tx_broadcast_total = Counter(
    "payouts_tx_broadcast_total",
    "Transfer broadcasts by outcome.",
    ["result"],  # "success" | "failed"
    registry=registry,
)

tx_duplicate_total = Counter(
    "payouts_tx_duplicate_total",
    "Requests served from the idempotency cache (no new broadcast).",
    registry=registry,
)

tx_rejected_total = Counter(
    "payouts_tx_rejected_total",
    "Requests rejected before broadcast.",
    ["reason"],  # "insufficient_usdt" | "insufficient_trx" | "energy_too_expensive" | "invalid_address" | "rpc_failed"
    registry=registry,
)

rate_limit_drops_total = Counter(
    "payouts_rate_limit_drops_total",
    "Requests dropped by the rate limiter (HTTP 429).",
    registry=registry,
)

idempotency_conflicts_total = Counter(
    "payouts_idempotency_conflicts_total",
    "Timed-out waits for an in-flight idempotency key (HTTP 409 CONFLICT).",
    registry=registry,
)

idempotency_unresolved_total = Counter(
    "payouts_idempotency_unresolved_total",
    "Requests hitting a key in UNKNOWN state — prior process crashed "
    "mid-broadcast. Requires operator reconciliation.",
    registry=registry,
)

http_requests_total = Counter(
    "payouts_http_requests_total",
    "Total HTTP requests by method, path, and status class.",
    ["method", "path", "status"],
    registry=registry,
)

# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------

tx_duration_seconds = Histogram(
    "payouts_tx_duration_seconds",
    "End-to-end /send duration, including balance checks and broadcast.",
    ["result"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
    registry=registry,
)

energy_estimated = Histogram(
    "payouts_energy_estimated",
    "Pre-flight energy estimate per transfer (energy units).",
    buckets=(5_000, 10_000, 13_000, 20_000, 32_000, 50_000, 100_000, 200_000),
    registry=registry,
)

energy_burn_trx_estimated = Histogram(
    "payouts_energy_burn_trx_estimated",
    "Estimated TRX that would be burned if no energy is available.",
    buckets=(0.1, 1, 5, 10, 13, 20, 30, 50),
    registry=registry,
)

fee_limit_sun = Histogram(
    "payouts_fee_limit_sun",
    "fee_limit (sun) chosen for each broadcast.",
    buckets=(5_000_000, 7_000_000, 10_000_000, 15_000_000, 20_000_000, 30_000_000),
    registry=registry,
)

# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------

trx_balance_gauge = Gauge(
    "payouts_trx_balance",
    "Current TRX balance of the signing wallet (from the last /balance or /send hit).",
    registry=registry,
)
usdt_balance_gauge = Gauge(
    "payouts_usdt_balance",
    "Current USDT balance of the signing wallet.",
    registry=registry,
)
energy_available_gauge = Gauge(
    "payouts_energy_available",
    "Energy units currently available to the wallet (from stake).",
    registry=registry,
)
bandwidth_free_available_gauge = Gauge(
    "payouts_bandwidth_free_available",
    "Free daily bandwidth remaining (non-staked quota).",
    registry=registry,
)
bandwidth_paid_available_gauge = Gauge(
    "payouts_bandwidth_paid_available",
    "Staked/paid bandwidth remaining.",
    registry=registry,
)
tron_power_staked_gauge = Gauge(
    "payouts_tron_power_staked",
    "Total TRX staked by the wallet.",
    registry=registry,
)
node_connected_gauge = Gauge(
    "payouts_node_connected",
    "1 if the last health check reached TronGrid, else 0.",
    registry=registry,
)
uptime_gauge = Gauge(
    "payouts_uptime_seconds",
    "Uptime in seconds, updated by every /metrics scrape.",
    registry=registry,
)
idempotency_store_size_gauge = Gauge(
    "payouts_idempotency_store_size",
    "Number of keys currently tracked by the idempotency store.",
    registry=registry,
)

# ---------------------------------------------------------------------------
# Telegram operator alerts (1.9.0+)
# ---------------------------------------------------------------------------

alert_attempts_total = Counter(
    "skr_crypto_alert_attempts_total",
    "Telegram alert delivery attempts by outcome.",
    ["result"],  # "success" | "fail"
    registry=registry,
)
alert_giveup_total = Counter(
    "skr_crypto_alert_giveup_total",
    "Telegram alert deliveries that exhausted the retry budget.",
    registry=registry,
)
alert_queue_depth_gauge = Gauge(
    "skr_crypto_alert_queue_depth",
    "Pending Telegram alert deliveries waiting on the worker.",
    registry=registry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def record_resource_snapshot(summary: dict) -> None:
    """Update all resource-related gauges from a get_resource_summary dict."""
    energy_available_gauge.set(summary.get("energy_available", 0))
    bandwidth_free_available_gauge.set(summary.get("bandwidth_free_available", 0))
    bandwidth_paid_available_gauge.set(summary.get("bandwidth_paid_available", 0))
    tron_power_staked_gauge.set(summary.get("tron_power", 0))


def record_balances(trx, usdt) -> None:
    """Update balance gauges. Accepts Decimal, int, or float."""
    try:
        trx_balance_gauge.set(float(trx))
    except Exception:
        pass
    try:
        usdt_balance_gauge.set(float(usdt))
    except Exception:
        pass


def render() -> bytes:
    """Render the current registry as Prometheus text exposition format."""
    return generate_latest(registry)


# Expose the MIME type for the endpoint.
METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST
