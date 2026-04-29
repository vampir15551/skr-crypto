from __future__ import annotations

import os
import socket
import sys
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Key provider
# ---------------------------------------------------------------------------
# Selects the backend for loading the TRON treasury private key. See
# app/key_providers.py for backend semantics. Valid values:
#   - 1password : 1Password CLI (`op`); default for operator-on-laptop
#   - env       : raw hex in PRIVATE_KEY_HEX env var; for containers
#   - file      : raw hex in a chmod-600 file at PRIVATE_KEY_FILE
#   - keychain  : macOS Keychain (KEYCHAIN_SERVICE/KEYCHAIN_ACCOUNT)
KEY_PROVIDER: str = os.getenv("KEY_PROVIDER", "1password").strip().lower()

# 1Password backend
OP_VAULT: str = os.getenv("OP_VAULT", "Treasury")
OP_ITEM: str = os.getenv("OP_ITEM", "TRON-Treasury-TXpdZ6qv")
OP_FIELD: str = os.getenv("OP_FIELD", "password")

# File backend
PRIVATE_KEY_FILE: str = os.getenv("PRIVATE_KEY_FILE", "")

# macOS Keychain backend
KEYCHAIN_SERVICE: str = os.getenv("KEYCHAIN_SERVICE", "payouts")
KEYCHAIN_ACCOUNT: str = os.getenv("KEYCHAIN_ACCOUNT", "treasury")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_TOKEN: str = os.getenv("AUTH_TOKEN", "")

# ---------------------------------------------------------------------------
# TRON
# ---------------------------------------------------------------------------
TRON_NETWORK: str = os.getenv("TRON_NETWORK", "mainnet")
TRONGRID_API_KEY: str = os.getenv("TRONGRID_API_KEY", "")
USDT_CONTRACT: str = os.getenv("USDT_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
USDT_DECIMALS: int = 6

# Per-tx fee_limit in SUN (1 TRX = 1_000_000 SUN). Default 30 TRX — covers a
# TRC-20 transfer on mainnet without any energy stake.
USDT_FEE_LIMIT_SUN: int = int(os.getenv("USDT_FEE_LIMIT_SUN", "30000000"))

# Minimum TRX balance required before we attempt a transfer. Must be >=
# USDT_FEE_LIMIT_SUN/1_000_000 with a safety margin, otherwise the tx will
# fail on-chain with OUT_OF_ENERGY / fee_limit_exceeded.
try:
    MIN_TRX_RESERVE: Decimal = Decimal(os.getenv("MIN_TRX_RESERVE", "50"))
except InvalidOperation:
    MIN_TRX_RESERVE = Decimal("50")

# HTTP timeout for every RPC call to TronGrid (seconds).
TRON_HTTP_TIMEOUT: int = int(os.getenv("TRON_HTTP_TIMEOUT", "15"))

# ---------------------------------------------------------------------------
# Cost / energy controls
# ---------------------------------------------------------------------------
# These let the operator put a hard ceiling on the TRX burned per transfer,
# and to refuse a transfer outright if its estimated cost is too high — which
# in practice forces them to either stake TRX for energy or rent it on a
# marketplace, both of which are 10–100x cheaper than burning.

# Mainnet "energy fee" (sun per energy unit). Read from chain at startup
# (get_chain_parameters key 11), this is a fallback if the chain query fails.
# Historical range: 280 → 420 → 100 (proposal-controlled). 420 is a safe default.
TRON_ENERGY_PRICE_SUN_FALLBACK: int = int(os.getenv("TRON_ENERGY_PRICE_SUN_FALLBACK", "420"))

# Hard ceiling on TRX we're willing to burn for energy on a single transfer.
# If the on-chain estimate exceeds this, the request is rejected with
# code=ENERGY_TOO_EXPENSIVE — the operator should stake/rent instead.
# 0 disables the check (you'll burn whatever fee_limit allows).
try:
    MAX_ENERGY_BURN_TRX: Decimal = Decimal(os.getenv("MAX_ENERGY_BURN_TRX", "20"))
except InvalidOperation:
    MAX_ENERGY_BURN_TRX = Decimal("20")

# Multiplier applied to the energy estimate when computing the per-tx
# fee_limit. >1 gives headroom for an estimate that under-counts (e.g. a
# concurrent state change on the contract). The fee_limit is still capped at
# USDT_FEE_LIMIT_SUN.
try:
    FEE_LIMIT_SAFETY_MULT: Decimal = Decimal(os.getenv("FEE_LIMIT_SAFETY_MULT", "1.3"))
except InvalidOperation:
    FEE_LIMIT_SAFETY_MULT = Decimal("1.3")

# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
# When set, audit records are also written line-buffered + fsync'd to this
# file. Stdout audit logging happens regardless. Use a path on a durable
# volume in production.
AUDIT_LOG_FILE: str = os.getenv("AUDIT_LOG_FILE", "")

# ---------------------------------------------------------------------------
# Idempotency persistence
# ---------------------------------------------------------------------------
# Path to a SQLite file. When set, the idempotency store survives process
# restarts: a key that was committed in a previous run still returns the
# cached txid as a duplicate. Orphaned "pending" rows from crashed prior
# runs are promoted to "unknown" at startup — retries on those keys get a
# 409 IDEMPOTENCY_UNRESOLVED, forcing the operator to reconcile manually
# (check tronscan) before retrying with the same key.
#
# Empty = in-memory only (state lost on restart). Use a path on a durable
# volume in production.
IDEMPOTENCY_DB_PATH: str = os.getenv("IDEMPOTENCY_DB_PATH", "")

# ---------------------------------------------------------------------------
# Trusted reverse proxies (for X-Forwarded-For rate limiting)
# ---------------------------------------------------------------------------
# Comma-separated list of IP addresses. If a request's direct peer is in
# this list, the rate limiter and request log read the first hop from
# X-Forwarded-For instead. Empty means "always use the direct peer" — safe
# default: spoofed XFF headers are ignored.
TRUSTED_PROXIES: tuple[str, ...] = tuple(
    p.strip() for p in os.getenv("TRUSTED_PROXIES", "").split(",") if p.strip()
)

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
SERVER_HOST: str = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT: int = int(os.getenv("SERVER_PORT", "8000"))
SHUTDOWN_TIMEOUT: int = int(os.getenv("SHUTDOWN_TIMEOUT", "600"))

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
RATE_LIMIT_MAX: int = int(os.getenv("RATE_LIMIT_MAX", "30"))
RATE_LIMIT_WINDOW: int = int(os.getenv("RATE_LIMIT_WINDOW", "60"))

# ---------------------------------------------------------------------------
# LAN detection
# ---------------------------------------------------------------------------
LAN_SUBNETS = ("192.168.88.", "192.168.89.")


def detect_lan_ip() -> str | None:
    """Return the first local IP that belongs to one of LAN_SUBNETS."""
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if any(ip.startswith(sub) for sub in LAN_SUBNETS):
                return ip
    except Exception:
        pass

    for probe in ("192.168.88.1", "192.168.89.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.1)
                s.connect((probe, 1))
                ip = s.getsockname()[0]
                if any(ip.startswith(sub) for sub in LAN_SUBNETS):
                    return ip
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

def validate_config() -> None:
    """Fail fast if critical config is missing or invalid."""
    errors: list[str] = []

    if not AUTH_TOKEN:
        errors.append("AUTH_TOKEN is not set")

    if KEY_PROVIDER not in ("1password", "env", "file", "keychain"):
        errors.append(
            f"KEY_PROVIDER must be one of 1password/env/file/keychain, "
            f"got: {KEY_PROVIDER}"
        )

    if KEY_PROVIDER == "file" and not PRIVATE_KEY_FILE:
        errors.append("KEY_PROVIDER=file requires PRIVATE_KEY_FILE")

    if TRON_NETWORK not in ("mainnet", "shasta", "nile"):
        errors.append(f"TRON_NETWORK must be mainnet/shasta/nile, got: {TRON_NETWORK}")

    if not USDT_CONTRACT.startswith("T") or len(USDT_CONTRACT) != 34:
        errors.append(f"USDT_CONTRACT is invalid: {USDT_CONTRACT}")

    if SHUTDOWN_TIMEOUT < 60:
        errors.append(f"SHUTDOWN_TIMEOUT too low: {SHUTDOWN_TIMEOUT}s (min 60)")

    if RATE_LIMIT_MAX < 1:
        errors.append(f"RATE_LIMIT_MAX must be >= 1, got: {RATE_LIMIT_MAX}")

    # Sanity: the TRX reserve must cover the on-chain fee_limit, otherwise the
    # transaction will fail with fee_limit_exceeded and we'll have a false
    # "successful" pre-check.
    fee_limit_trx = Decimal(USDT_FEE_LIMIT_SUN) / Decimal(1_000_000)
    if fee_limit_trx > MIN_TRX_RESERVE:
        errors.append(
            f"MIN_TRX_RESERVE ({MIN_TRX_RESERVE}) must be >= USDT_FEE_LIMIT_SUN/1_000_000 ({fee_limit_trx})"
        )

    if TRON_HTTP_TIMEOUT < 1:
        errors.append(f"TRON_HTTP_TIMEOUT must be >= 1, got: {TRON_HTTP_TIMEOUT}")

    if TRON_ENERGY_PRICE_SUN_FALLBACK < 1:
        errors.append(
            f"TRON_ENERGY_PRICE_SUN_FALLBACK must be >= 1, got: {TRON_ENERGY_PRICE_SUN_FALLBACK}"
        )

    if MAX_ENERGY_BURN_TRX < 0:
        errors.append(f"MAX_ENERGY_BURN_TRX must be >= 0, got: {MAX_ENERGY_BURN_TRX}")

    if Decimal("1") > FEE_LIMIT_SAFETY_MULT:
        errors.append(
            f"FEE_LIMIT_SAFETY_MULT must be >= 1.0, got: {FEE_LIMIT_SAFETY_MULT}"
        )

    if errors:
        for e in errors:
            print(f"CONFIG ERROR: {e}", file=sys.stderr)
        sys.exit(1)
