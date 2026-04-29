from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal

import base58
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from skr_crypto.server import audit, metrics
from skr_crypto.server.config import MAX_ENERGY_BURN_TRX, MIN_TRX_RESERVE, TRON_NETWORK
from skr_crypto.server.exceptions import (
    EnergyTooExpensive,
    InsufficientBalance,
    InvalidAddress,
    PayoutError,
    TransactionFailed,
)
from skr_crypto.server.idempotency import idempotency
from skr_crypto.server.models import (
    BalanceResponse,
    HealthLiveResponse,
    HealthResponse,
    SendRequest,
    SendResponse,
    VersionResponse,
)
from skr_crypto.server.net import client_address
from skr_crypto.server.security import verify_api_key
from skr_crypto.server.shutdown import seconds_remaining, uptime
from skr_crypto.server.tron_client import tron
from skr_crypto.server.version import GIT_SHA, START_TIME

log = logging.getLogger("payouts")

router = APIRouter(prefix="/api/v1")


# Process-wide serialization for /send. TronGrid free-tier QPS is tight
# enough that 2-3 concurrent sends burst over the limit (each /send issues
# 5-7 RPCs: balance, TRX balance, destination info, estimate, broadcast).
# A single in-flight send keeps us well under the ceiling and turns races
# into a queue — slower under load but no 429 cascades.
_send_lock = threading.Lock()


# --------------------------------------------------------------------------
# POST /api/v1/send
# --------------------------------------------------------------------------

@router.post("/send", response_model=SendResponse)
def send(req: SendRequest, request: Request, _: str = Depends(verify_api_key)):
    with _send_lock:
        return _send_impl(req, request)


def _send_impl(req: SendRequest, request: Request) -> SendResponse:
    client_ip = client_address(request)
    started = time.time()
    amount = req.amount

    log.info(
        "[SEND] Incoming | ip=%s to=%s amount=%s key=%s",
        client_ip, req.to_address, req.amount, req.idempotency_key,
    )

    # ── Validate address first (cheap, deterministic) ────────────────────
    # Done before reserving the idempotency slot so a malformed address
    # never poisons the store.
    _validate_tron_address(req.to_address)

    # ── Reserve the idempotency slot atomically ──────────────────────────
    # reserve() returns:
    #   None      → we own the slot, must commit() or release() it
    #   "<txid>"  → already committed by an earlier request (duplicate)
    #   blocks    → another request is in flight; waits for it then returns
    #               its txid (or raises IdempotencyConflict on timeout/failure)
    existing_txid = idempotency.reserve(req.idempotency_key)
    if existing_txid is not None:
        log.warning("[SEND] DUPLICATE | key=%s txid=%s", req.idempotency_key, existing_txid)
        audit.record(
            "SEND_DUPLICATE",
            from_address=tron.address,
            to_address=req.to_address,
            amount=str(req.amount),
            txid=existing_txid,
            idempotency_key=req.idempotency_key,
            client_ip=client_ip,
            result="duplicate",
        )
        metrics.tx_duplicate_total.inc()
        return SendResponse(
            txid=existing_txid,
            from_address=tron.address,
            to_address=req.to_address,
            amount=str(req.amount),
            idempotency_key=req.idempotency_key,
            status="duplicate",
        )

    # From here on, we hold the reservation. Anything that aborts before
    # commit() must release() it, otherwise future retries with the same
    # key will block until the wait timeout.
    try:
        # Preflight RPCs (balance / TRX / destination / estimate) are wrapped
        # so any transient TronGrid failure (429, 5xx, timeout, connection
        # reset) lands in audit.log as SEND_FAILED rpc_failed instead of
        # escaping as an unhandled HTTPError → ASGI traceback → opaque 500.
        # Structured rejections (InsufficientBalance, EnergyTooExpensive)
        # raise PayoutError and are re-raised untouched so their own
        # SEND_REJECTED audit + 400 handler still apply.
        try:
            # ── Check USDT balance ───────────────────────────────────────
            balance = tron.get_usdt_balance()
            if balance < amount:
                audit.record(
                    "SEND_REJECTED",
                    from_address=tron.address,
                    to_address=req.to_address,
                    amount=str(amount),
                    idempotency_key=req.idempotency_key,
                    client_ip=client_ip,
                    result="insufficient_usdt",
                    details=f"have={balance}",
                )
                metrics.tx_rejected_total.labels(reason="insufficient_usdt").inc()
                raise InsufficientBalance("USDT", str(balance), str(amount))

            # ── Check TRX for fees ───────────────────────────────────────
            trx_balance = tron.get_trx_balance()
            if trx_balance < MIN_TRX_RESERVE:
                audit.record(
                    "SEND_REJECTED",
                    from_address=tron.address,
                    to_address=req.to_address,
                    amount=str(amount),
                    idempotency_key=req.idempotency_key,
                    client_ip=client_ip,
                    result="insufficient_trx",
                    details=f"have={trx_balance}",
                )
                metrics.tx_rejected_total.labels(reason="insufficient_trx").inc()
                raise InsufficientBalance("TRX", str(trx_balance), str(MIN_TRX_RESERVE))

            # Keep balance gauges fresh any time we hit get_*_balance in the
            # hot path — lets /metrics scrapers see current state without a
            # dedicated RPC poll of their own.
            metrics.record_balances(trx_balance, balance)

            # ── Pre-flight: destination warmth & activation ──────────────
            # Diagnostic only — best-effort, never blocks the send. Tagging
            # cold (zero USDT balance) vs warm makes the ~13k-vs-32k energy
            # split obvious in logs and explains retry-flake patterns where
            # the same key sometimes burns out and sometimes lands.
            dest_info = tron.get_destination_info(req.to_address)
            recipient_warmth = "cold" if dest_info["usdt_balance"] == 0 else "warm"
            log.info(
                "[SEND] Destination | to=%s activated=%s trx=%s usdt=%s warmth=%s",
                req.to_address, dest_info["exists"],
                dest_info["trx_balance"], dest_info["usdt_balance"], recipient_warmth,
            )

            # ── Pre-flight energy estimate + dynamic fee_limit ───────────
            # Two goals:
            #   (1) Cap the worst-case burn: if a contract storage hot spot
            #       suddenly costs 100k energy, we only burn what fee_limit
            #       allows, not the full USDT_FEE_LIMIT_SUN.
            #   (2) Refuse outright if even the estimated burn exceeds the
            #       operator's MAX_ENERGY_BURN_TRX — forces them to stake/rent
            #       instead of silently bleeding TRX.
            estimated_energy = tron.estimate_transfer_energy(req.to_address, amount)
            estimated_burn_trx: Decimal | None = None
            if estimated_energy:
                energy_price = tron.energy_price_sun()
                estimated_burn_trx = (
                    Decimal(estimated_energy) * Decimal(energy_price) / Decimal(1_000_000)
                )
                metrics.energy_estimated.observe(estimated_energy)
                metrics.energy_burn_trx_estimated.observe(float(estimated_burn_trx))
                log.info(
                    "[SEND] Estimate | energy=%d price=%d sun/u burn~%s TRX recipient=%s",
                    estimated_energy, energy_price, estimated_burn_trx, recipient_warmth,
                )
                if MAX_ENERGY_BURN_TRX > 0 and estimated_burn_trx > MAX_ENERGY_BURN_TRX:
                    audit.record(
                        "SEND_REJECTED",
                        from_address=tron.address,
                        to_address=req.to_address,
                        amount=str(amount),
                        idempotency_key=req.idempotency_key,
                        client_ip=client_ip,
                        result="energy_too_expensive",
                        details=(
                            f"energy={estimated_energy} burn={estimated_burn_trx} TRX "
                            f"limit={MAX_ENERGY_BURN_TRX} TRX"
                        ),
                    )
                    metrics.tx_rejected_total.labels(reason="energy_too_expensive").inc()
                    raise EnergyTooExpensive(
                        estimated_energy, estimated_burn_trx, MAX_ENERGY_BURN_TRX,
                    )
            else:
                log.warning(
                    "[SEND] Estimate unavailable | recipient=%s — using fee_limit ceiling, "
                    "OUT_OF_ENERGY risk if recipient is cold",
                    recipient_warmth,
                )

            fee_limit_sun = tron.compute_fee_limit_sun(estimated_energy)
            metrics.fee_limit_sun.observe(fee_limit_sun)
        except PayoutError:
            raise
        except Exception as exc:
            elapsed = time.time() - started
            log.error(
                "[SEND] PREFLIGHT_FAILED | %s: %s | to=%s amount=%s elapsed=%.2fs",
                type(exc).__name__, exc, req.to_address, amount, elapsed,
            )
            audit.record(
                "SEND_FAILED",
                from_address=tron.address,
                to_address=req.to_address,
                amount=str(amount),
                idempotency_key=req.idempotency_key,
                client_ip=client_ip,
                result="rpc_failed",
                details=f"{type(exc).__name__}: {exc}",
            )
            metrics.tx_rejected_total.labels(reason="rpc_failed").inc()
            raise TransactionFailed(f"pre-broadcast RPC failed: {exc}")

        # ── Broadcast transaction ────────────────────────────────────────
        log.info(
            "[SEND] Broadcasting | %s -> %s amount=%s USDT fee_limit=%d sun",
            tron.address, req.to_address, amount, fee_limit_sun,
        )

        try:
            txid = tron.send_usdt(req.to_address, amount, fee_limit_sun=fee_limit_sun)
        except Exception as exc:
            elapsed = time.time() - started
            log.error(
                "[SEND] FAILED | %s: %s | to=%s amount=%s elapsed=%.2fs",
                type(exc).__name__, exc, req.to_address, amount, elapsed,
            )
            audit.record(
                "SEND_FAILED",
                from_address=tron.address,
                to_address=req.to_address,
                amount=str(amount),
                idempotency_key=req.idempotency_key,
                client_ip=client_ip,
                result="tx_failed",
                details=str(exc),
            )
            metrics.tx_broadcast_total.labels(result="failed").inc()
            metrics.tx_duration_seconds.labels(result="failed").observe(elapsed)
            raise TransactionFailed(str(exc))

        # ── Commit idempotency only after a successful broadcast ─────────
        idempotency.commit(req.idempotency_key, txid)

    except BaseException:
        # Any failure between reserve and commit must release the slot.
        # commit() above replaces PENDING with the real txid; release() is a
        # no-op on already-committed slots, so this is safe even on the
        # success path if commit() somehow raised.
        idempotency.release(req.idempotency_key)
        raise

    # ── Success ──────────────────────────────────────────────────────────
    elapsed = time.time() - started

    audit.record(
        "SEND_SUCCESS",
        from_address=tron.address,
        to_address=req.to_address,
        amount=str(amount),
        txid=txid,
        idempotency_key=req.idempotency_key,
        client_ip=client_ip,
        result="broadcast",
        details=f"elapsed={elapsed:.2f}s",
    )

    log.info(
        "[SEND] SUCCESS | txid=%s %s -> %s amount=%s USDT elapsed=%.2fs",
        txid, tron.address, req.to_address, amount, elapsed,
    )

    metrics.tx_broadcast_total.labels(result="success").inc()
    metrics.tx_duration_seconds.labels(result="success").observe(elapsed)
    metrics.idempotency_store_size_gauge.set(idempotency.count())

    return SendResponse(
        txid=txid,
        from_address=tron.address,
        to_address=req.to_address,
        amount=str(amount),
        idempotency_key=req.idempotency_key,
    )


def _validate_tron_address(address: str) -> None:
    """Validate TRON base58check address format."""
    try:
        if not address.startswith("T"):
            raise InvalidAddress(address, "must start with T")

        try:
            decoded = base58.b58decode_check(address)
        except Exception:
            raise InvalidAddress(address, "invalid base58check encoding")

        if len(decoded) != 21:
            raise InvalidAddress(address, "decoded length must be 21 bytes")

        if decoded[0] != 0x41:
            raise InvalidAddress(address, "invalid TRON address prefix byte")
    except InvalidAddress:
        metrics.tx_rejected_total.labels(reason="invalid_address").inc()
        raise


# --------------------------------------------------------------------------
# GET /api/v1/balance
# --------------------------------------------------------------------------

@router.get("/balance", response_model=BalanceResponse)
def balance(request: Request, _: str = Depends(verify_api_key)):
    client_ip = client_address(request)
    log.info("[BALANCE] Request from %s", client_ip)

    trx = tron.get_trx_balance()
    usdt = tron.get_usdt_balance()
    res = tron.get_resource_summary()

    log.info(
        "[BALANCE] address=%s TRX=%s USDT=%s energy=%d bw_free=%d bw_paid=%d staked=%d",
        tron.address, trx, usdt,
        res["energy_available"], res["bandwidth_free_available"],
        res["bandwidth_paid_available"], res["tron_power"],
    )

    # Refresh Prometheus gauges — /balance is the natural refresh source for
    # dashboards; /metrics scrapes then see fresh values without a separate
    # TronGrid RPC.
    metrics.record_balances(trx, usdt)
    metrics.record_resource_snapshot(res)

    return BalanceResponse(
        address=tron.address,
        trx=str(trx),
        usdt=str(usdt),
        energy_available=res["energy_available"],
        bandwidth_free_available=res["bandwidth_free_available"],
        bandwidth_paid_available=res["bandwidth_paid_available"],
        tron_power_staked=res["tron_power"],
    )


# --------------------------------------------------------------------------
# GET /api/v1/health/live  — unauthenticated liveness probe (k8s, LB)
# GET /api/v1/health       — full readiness probe (auth + RPC)
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# GET /api/v1/metrics — Prometheus scrape endpoint
# --------------------------------------------------------------------------

@router.get("/metrics")
def metrics_endpoint(_: str = Depends(verify_api_key)):
    """Prometheus text exposition.

    Requires the same X-API-Key as every other endpoint — scrapers must
    carry the token. This avoids leaking balances and traffic patterns to
    anyone who can reach the service on the network. The scrape itself
    does NO RPC calls; gauges are refreshed by the normal /balance,
    /health, and /send paths.
    """
    # Uptime gauge is the one value that's always fresh and free.
    metrics.uptime_gauge.set(uptime())
    metrics.idempotency_store_size_gauge.set(idempotency.count())
    return Response(
        content=metrics.render(),
        media_type=metrics.METRICS_CONTENT_TYPE,
    )


@router.get("/health/live", response_model=HealthLiveResponse)
def health_live():
    """Unauthenticated liveness probe.

    No external calls — does NOT prove the TronGrid endpoint is reachable.
    For that, use /api/v1/health (authenticated).
    """
    return HealthLiveResponse(uptime_seconds=uptime())


@router.get("/version", response_model=VersionResponse)
def version():
    """Build/version info — unauthenticated.

    Intentionally cheap and unauthenticated so dashboards / deploy
    pipelines can poll it without having to carry the API key. Reveals
    only the git sha and process start time, neither of which is
    sensitive (the sha is recorded in git history; start time is
    inferable from the access log).
    """
    return VersionResponse(
        git_sha=GIT_SHA,
        started_at=START_TIME,
        network=TRON_NETWORK,
    )


@router.get("/health", response_model=HealthResponse)
def health(request: Request, _: str = Depends(verify_api_key)):
    client_ip = client_address(request)
    log.info("[HEALTH] Request from %s", client_ip)

    node_ok = tron.check_connection()
    res = tron.get_resource_summary() if node_ok else {
        "energy_available": 0,
        "bandwidth_free_available": 0,
        "bandwidth_paid_available": 0,
        "tron_power": 0,
    }

    metrics.node_connected_gauge.set(1.0 if node_ok else 0.0)
    if node_ok:
        metrics.record_resource_snapshot(res)

    return HealthResponse(
        address=tron.address,
        network=TRON_NETWORK,
        uptime_seconds=uptime(),
        shutdown_in_seconds=seconds_remaining(),
        node_connected=node_ok,
        energy_available=res["energy_available"],
        bandwidth_free_available=res["bandwidth_free_available"],
        bandwidth_paid_available=res["bandwidth_paid_available"],
        tron_power_staked=res["tron_power"],
    )
