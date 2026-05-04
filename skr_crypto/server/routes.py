from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal

import base58
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from skr_crypto.server import audit, metrics
from skr_crypto.server.config import (
    MAX_ENERGY_BURN_TRX,
    MIN_TRX_RESERVE,
    RISK_BLOCK_LEVEL,
    RISK_USE_EXTERNAL,
    TRON_NETWORK,
)
from skr_crypto.server.exceptions import (
    EnergyTooExpensive,
    InsufficientBalance,
    InvalidAddress,
    PayoutError,
    RiskTooHigh,
    TransactionFailed,
    WalletAutoPickFailed,
    WalletNotFoundError,
    WalletPoolEmptyError,
)
from skr_crypto.server.idempotency import idempotency
from skr_crypto.server.models import (
    BalanceResponse,
    HealthLiveResponse,
    HealthResponse,
    SendRequest,
    SendResponse,
    VersionResponse,
    WalletListResponse,
    WalletSummary,
)
from skr_crypto.server.net import client_address
from skr_crypto.server.risk import assess_risk, should_block
from skr_crypto.server.security import require_scope
from skr_crypto.server.shutdown import seconds_remaining, uptime
from skr_crypto.server.tron_client import tron
from skr_crypto.server.version import GIT_SHA, START_TIME
from skr_crypto.server.wallet_pool import (
    WalletAutoPickFailed as PoolAutoPickFailed,
)
from skr_crypto.server.wallet_pool import (
    WalletNotFound as PoolWalletNotFound,
)
from skr_crypto.server.wallet_pool import (
    WalletPoolEmpty as PoolEmpty,
)
from skr_crypto.server.wallet_pool import (
    wallets,
)

log = logging.getLogger("payouts")

router = APIRouter(prefix="/api/v1")


# Process-wide serialization for /send. TronGrid free-tier QPS is tight
# enough that 2-3 concurrent sends burst over the limit (each /send issues
# 5-7 RPCs: balance, TRX balance, destination info, estimate, broadcast).
# A single in-flight send keeps us well under the ceiling and turns races
# into a queue — slower under load but no 429 cascades. The lock is global
# rather than per-wallet because the rate-limit ceiling is shared.
_send_lock = threading.Lock()


def _resolve_wallet(name: str | None):
    """Translate WalletPool errors into PayoutError subclasses.

    Endpoints that resolve a wallet (``/send``, ``/balance``) do this so
    the FastAPI exception handlers can return precise 4xx codes instead
    of an opaque 500.
    """
    try:
        return wallets.resolve(name, tron_client=tron)
    except PoolWalletNotFound as exc:
        raise WalletNotFoundError(exc.name, exc.available)
    except PoolEmpty:
        raise WalletPoolEmptyError()
    except PoolAutoPickFailed as exc:
        raise WalletAutoPickFailed(str(exc))


# --------------------------------------------------------------------------
# POST /api/v1/send
# --------------------------------------------------------------------------

@router.post("/send", response_model=SendResponse)
def send(
    req: SendRequest,
    request: Request,
    token_id: str = Depends(require_scope("send")),
):
    request.state.token_id = token_id
    with _send_lock:
        return _send_impl(req, request)


def _send_impl(req: SendRequest, request: Request) -> SendResponse:
    client_ip = client_address(request)
    token_id = getattr(request.state, "token_id", "")
    started = time.time()
    amount = req.amount

    log.info(
        "[SEND] Incoming | ip=%s token=%s wallet=%s to=%s amount=%s key=%s",
        client_ip, token_id, req.wallet or "<auto>", req.to_address, req.amount,
        req.idempotency_key,
    )

    # ── Resolve source wallet first ──────────────────────────────────────
    # Either the explicit name was given (must exist) or auto-pick by
    # max USDT. Errors here turn into 400/404 — see exception handlers.
    wallet = _resolve_wallet(req.wallet)
    log.info("[SEND] Using wallet | name=%s address=%s", wallet.name, wallet.address)

    # ── Validate destination address ─────────────────────────────────────
    # Done before reserving the idempotency slot so a malformed address
    # never poisons the store.
    _validate_tron_address(req.to_address)

    # ── Recipient risk preflight ─────────────────────────────────────────
    # Catches OFAC-sanctioned addresses, Tether blacklist, smart-contract
    # destinations, known-burn patterns, and unactivated accounts BEFORE
    # we burn fee_limit on a doomed broadcast.
    risk_report = None
    risk_level_str = "skipped"
    if RISK_BLOCK_LEVEL != "none":
        risk_report = assess_risk(req.to_address, external=RISK_USE_EXTERNAL)
        risk_level_str = risk_report.level.value
        log.info(
            "[SEND] Risk | level=%s checks=%s",
            risk_level_str,
            ",".join(
                f"{c.name}:{c.status.value}"
                for c in risk_report.checks
                if c.status.value != "ok"
            ) or "all-ok",
        )
        if should_block(risk_report.level, RISK_BLOCK_LEVEL):
            failed = [
                c.name for c in risk_report.checks if c.status.value == "fail"
            ]
            audit.record(
                "SEND_REJECTED",
                wallet=wallet.name,
                from_address=wallet.address,
                to_address=req.to_address,
                amount=str(amount),
                idempotency_key=req.idempotency_key,
                client_ip=client_ip,
                token_id=token_id,
                result="risk_too_high",
                details=(
                    f"level={risk_level_str} "
                    f"failed={','.join(failed) or 'none'}"
                ),
            )
            metrics.tx_rejected_total.labels(reason="risk_too_high").inc()
            raise RiskTooHigh(
                level=risk_level_str,
                report=risk_report.to_dict(),
            )

    # ── Reserve the idempotency slot atomically ──────────────────────────
    existing_txid = idempotency.reserve(req.idempotency_key)
    if existing_txid is not None:
        log.warning("[SEND] DUPLICATE | key=%s txid=%s", req.idempotency_key, existing_txid)
        audit.record(
            "SEND_DUPLICATE",
            wallet=wallet.name,
            from_address=wallet.address,
            to_address=req.to_address,
            amount=str(req.amount),
            txid=existing_txid,
            idempotency_key=req.idempotency_key,
            client_ip=client_ip,
            token_id=token_id,
            result="duplicate",
        )
        metrics.tx_duplicate_total.inc()
        return SendResponse(
            txid=existing_txid,
            from_address=wallet.address,
            wallet=wallet.name,
            to_address=req.to_address,
            amount=str(req.amount),
            idempotency_key=req.idempotency_key,
            status="duplicate",
        )

    try:
        try:
            # ── Check USDT balance ───────────────────────────────────────
            balance = tron.get_usdt_balance_for(wallet.address)
            if balance < amount:
                audit.record(
                    "SEND_REJECTED",
                    wallet=wallet.name,
                    from_address=wallet.address,
                    to_address=req.to_address,
                    amount=str(amount),
                    idempotency_key=req.idempotency_key,
                    client_ip=client_ip,
                    token_id=token_id,
                    result="insufficient_usdt",
                    details=f"have={balance}",
                )
                metrics.tx_rejected_total.labels(reason="insufficient_usdt").inc()
                raise InsufficientBalance("USDT", str(balance), str(amount))

            # ── Check TRX for fees ───────────────────────────────────────
            trx_balance = tron.get_trx_balance_for(wallet.address)
            if trx_balance < MIN_TRX_RESERVE:
                audit.record(
                    "SEND_REJECTED",
                    wallet=wallet.name,
                    from_address=wallet.address,
                    to_address=req.to_address,
                    amount=str(amount),
                    idempotency_key=req.idempotency_key,
                    client_ip=client_ip,
                    token_id=token_id,
                    result="insufficient_trx",
                    details=f"have={trx_balance}",
                )
                metrics.tx_rejected_total.labels(reason="insufficient_trx").inc()
                raise InsufficientBalance("TRX", str(trx_balance), str(MIN_TRX_RESERVE))

            metrics.record_balances(trx_balance, balance)

            # ── Pre-flight: destination warmth & activation ──────────────
            dest_info = tron.get_destination_info(req.to_address)
            recipient_warmth = "cold" if dest_info["usdt_balance"] == 0 else "warm"
            log.info(
                "[SEND] Destination | to=%s activated=%s trx=%s usdt=%s warmth=%s",
                req.to_address, dest_info["exists"],
                dest_info["trx_balance"], dest_info["usdt_balance"], recipient_warmth,
            )

            # ── Pre-flight energy estimate + dynamic fee_limit ───────────
            estimated_energy = tron.estimate_transfer_energy(
                wallet.address, req.to_address, amount,
            )
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
                        wallet=wallet.name,
                        from_address=wallet.address,
                        to_address=req.to_address,
                        amount=str(amount),
                        idempotency_key=req.idempotency_key,
                        client_ip=client_ip,
                        token_id=token_id,
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
                "[SEND] PREFLIGHT_FAILED | %s: %s | wallet=%s to=%s amount=%s elapsed=%.2fs",
                type(exc).__name__, exc, wallet.name, req.to_address, amount, elapsed,
            )
            audit.record(
                "SEND_FAILED",
                wallet=wallet.name,
                from_address=wallet.address,
                to_address=req.to_address,
                amount=str(amount),
                idempotency_key=req.idempotency_key,
                client_ip=client_ip,
                token_id=token_id,
                result="rpc_failed",
                details=f"{type(exc).__name__}: {exc}",
            )
            metrics.tx_rejected_total.labels(reason="rpc_failed").inc()
            raise TransactionFailed(f"pre-broadcast RPC failed: {exc}")

        # ── Broadcast transaction ────────────────────────────────────────
        log.info(
            "[SEND] Broadcasting | wallet=%s %s -> %s amount=%s USDT fee_limit=%d sun",
            wallet.name, wallet.address, req.to_address, amount, fee_limit_sun,
        )

        try:
            txid = wallet.send_usdt(
                tron.client,
                tron.get_usdt_contract(),
                req.to_address,
                amount,
                fee_limit_sun=fee_limit_sun,
            )
        except Exception as exc:
            elapsed = time.time() - started
            log.error(
                "[SEND] FAILED | %s: %s | wallet=%s to=%s amount=%s elapsed=%.2fs",
                type(exc).__name__, exc, wallet.name, req.to_address, amount, elapsed,
            )
            audit.record(
                "SEND_FAILED",
                wallet=wallet.name,
                from_address=wallet.address,
                to_address=req.to_address,
                amount=str(amount),
                idempotency_key=req.idempotency_key,
                client_ip=client_ip,
                token_id=token_id,
                result="tx_failed",
                details=str(exc),
            )
            metrics.tx_broadcast_total.labels(result="failed").inc()
            metrics.tx_duration_seconds.labels(result="failed").observe(elapsed)
            raise TransactionFailed(str(exc))

        idempotency.commit(req.idempotency_key, txid)

    except BaseException:
        idempotency.release(req.idempotency_key)
        raise

    # ── Success ──────────────────────────────────────────────────────────
    elapsed = time.time() - started

    audit.record(
        "SEND_SUCCESS",
        wallet=wallet.name,
        from_address=wallet.address,
        to_address=req.to_address,
        amount=str(amount),
        txid=txid,
        idempotency_key=req.idempotency_key,
        client_ip=client_ip,
        token_id=token_id,
        result="broadcast",
        details=f"elapsed={elapsed:.2f}s risk={risk_level_str}",
    )

    log.info(
        "[SEND] SUCCESS | txid=%s wallet=%s %s -> %s amount=%s USDT risk=%s elapsed=%.2fs",
        txid, wallet.name, wallet.address, req.to_address, amount, risk_level_str, elapsed,
    )

    metrics.tx_broadcast_total.labels(result="success").inc()
    metrics.tx_duration_seconds.labels(result="success").observe(elapsed)
    metrics.idempotency_store_size_gauge.set(idempotency.count())

    return SendResponse(
        txid=txid,
        from_address=wallet.address,
        wallet=wallet.name,
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
def balance(
    request: Request,
    wallet: str | None = Query(
        default=None,
        description="Wallet name (omit for auto-pick by max USDT)",
        max_length=64,
    ),
    token_id: str = Depends(require_scope("read")),
):
    request.state.token_id = token_id
    client_ip = client_address(request)
    log.info("[BALANCE] Request from %s | wallet=%s", client_ip, wallet or "<auto>")

    w = _resolve_wallet(wallet)
    trx = tron.get_trx_balance_for(w.address)
    usdt = tron.get_usdt_balance_for(w.address)
    res = tron.get_resource_summary_for(w.address)

    log.info(
        "[BALANCE] wallet=%s address=%s TRX=%s USDT=%s energy=%d bw_free=%d bw_paid=%d staked=%d",
        w.name, w.address, trx, usdt,
        res["energy_available"], res["bandwidth_free_available"],
        res["bandwidth_paid_available"], res["tron_power"],
    )

    metrics.record_balances(trx, usdt)
    metrics.record_resource_snapshot(res)

    return BalanceResponse(
        wallet=w.name,
        address=w.address,
        trx=str(trx),
        usdt=str(usdt),
        energy_available=res["energy_available"],
        bandwidth_free_available=res["bandwidth_free_available"],
        bandwidth_paid_available=res["bandwidth_paid_available"],
        tron_power_staked=res["tron_power"],
    )


# --------------------------------------------------------------------------
# GET /api/v1/wallets — list every configured wallet (multi-wallet awareness)
# --------------------------------------------------------------------------

@router.get("/wallets", response_model=WalletListResponse)
def wallets_endpoint(
    request: Request,
    token_id: str = Depends(require_scope("read")),
):
    request.state.token_id = token_id
    """Return every wallet in the pool with live balances + the auto-pick
    winner. Cost: N TRX-balance + N USDT-balance + N resource RPCs. For a
    single wallet this is the same cost as /balance.

    Wallets whose RPCs fail are still included, with empty/zero fields and
    ``trx="error"``/``usdt="error"`` so the operator can see something is
    wrong without /wallets itself returning 5xx."""
    client_ip = client_address(request)
    log.info("[WALLETS] Request from %s | count=%d", client_ip, wallets.count())

    out: list[WalletSummary] = []
    best_name: str | None = None
    best_usdt: Decimal = Decimal(-1)
    for w in wallets.all():
        try:
            trx = tron.get_trx_balance_for(w.address)
            usdt = tron.get_usdt_balance_for(w.address)
            res = tron.get_resource_summary_for(w.address)
        except Exception as exc:
            log.warning(
                "/wallets: balance lookup failed for %s (%s): %s",
                w.name, type(exc).__name__, exc,
            )
            out.append(WalletSummary(
                wallet=w.name,
                address=w.address,
                trx="error",
                usdt="error",
            ))
            continue
        out.append(WalletSummary(
            wallet=w.name,
            address=w.address,
            trx=str(trx),
            usdt=str(usdt),
            energy_available=res["energy_available"],
            bandwidth_free_available=res["bandwidth_free_available"],
            bandwidth_paid_available=res["bandwidth_paid_available"],
        ))
        if usdt > best_usdt:
            best_name = w.name
            best_usdt = usdt

    return WalletListResponse(wallets=out, auto_pick=best_name)


# --------------------------------------------------------------------------
# GET /api/v1/audit — paginated audit reader (1.8.0+, used by the web UI)
# --------------------------------------------------------------------------

@router.get("/audit")
def audit_endpoint(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    before_id: str | None = Query(default=None, max_length=40),
    event: str | None = Query(default=None, max_length=64),
    token_id: str = Depends(require_scope("read")),
):
    """Paginated audit-log reader. Returns the most recent records first.

    Reads ``AUDIT_LOG_FILE`` (the durable JSON-line log). Empty when
    the file isn't configured. ``before_id`` lets the UI page through
    older records; ``event`` filters by event name.

    Cost: O(file_size) per request — the audit file is rewound + scanned
    each time. For multi-GB log files this is slow. Operators that need
    high-throughput reads should grep / tail the file directly. The
    endpoint exists for the UI's tabular view + light operational use.
    """
    from skr_crypto.server.config import AUDIT_LOG_FILE
    request.state.token_id = token_id
    if not AUDIT_LOG_FILE:
        return {"records": [], "warning": "AUDIT_LOG_FILE not configured"}
    import json as _json
    import os as _os
    if not _os.path.exists(AUDIT_LOG_FILE):
        return {"records": []}
    out: list[dict] = []
    try:
        with open(AUDIT_LOG_FILE, encoding="utf-8") as fp:
            # Cheap approach for v1: read everything, slice. Files
            # past ~50MB will need a more efficient scan; that's a
            # future enhancement.
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if event and rec.get("event") != event:
                    continue
                out.append(rec)
    except OSError as exc:
        log.warning("[AUDIT] read failed: %s", exc)
        return {"records": [], "error": str(exc)}
    # Sort by id descending (most recent first)
    out.sort(key=lambda r: r.get("id", ""), reverse=True)
    if before_id:
        out = [r for r in out if r.get("id", "") < before_id]
    return {"records": out[:limit], "total_seen": len(out)}


# --------------------------------------------------------------------------
# GET /api/v1/tx/{txid}/status — postmortem on-chain status (1.6.0+)
# --------------------------------------------------------------------------

@router.get("/tx/{txid}/status")
def tx_status_endpoint(
    txid: str,
    request: Request,
    token_id: str = Depends(require_scope("read")),
):
    """Look up the on-chain status of a previously broadcast txid.

    This is the postmortem-poller's read API — see ADR 0010. Status
    becomes terminal asynchronously (typically within ~60s of the
    block landing). Until the poller resolves it, ``status`` is
    ``null`` and the caller should poll back later.

    Always returns 200 (even for unknown txids — they map to
    ``status: null, known: false``)."""
    from skr_crypto.server.receipt_poller import poller
    if poller is None:
        return {
            "txid": txid,
            "known": False,
            "status": None,
            "message": "receipt poller not running",
        }
    rec = poller.get_status(txid)
    if rec is None:
        return {
            "txid": txid,
            "known": False,
            "status": None,
        }
    return {
        "txid": rec.txid,
        "known": True,
        "status": rec.status,           # None until terminal
        "block_number": rec.block_number,
        "idempotency_key": rec.idempotency_key,
        "first_seen": rec.first_seen,
        "last_checked": rec.last_checked,
        "resolved_at": rec.resolved_at,
        "last_error": rec.last_error,
    }


# --------------------------------------------------------------------------
# GET /api/v1/risk/{address}  — recipient risk look-up (read-only)
# --------------------------------------------------------------------------

@router.get("/risk/{address}")
def risk_endpoint(
    address: str,
    request: Request,
    external: bool = False,
    token_id: str = Depends(require_scope("read")),
):
    """Run all wallet-risk checks against ``address``.

    Read-only: never mutates state, never broadcasts. Same logic the
    /send preflight uses. ``external=true`` queries the TronScan
    reputation API (~+300ms); off by default for speed.

    Always returns 200 with a JSON report whose ``level`` field tells
    you the verdict (``low`` / ``medium`` / ``high`` / ``invalid``).
    """
    client_ip = client_address(request)
    log.info(
        "[RISK] %s addr=%s external=%s", client_ip, address, external,
    )
    report = assess_risk(address, external=external)
    log.info(
        "[RISK] result | addr=%s level=%s",
        address, report.level.value,
    )
    return report.to_dict()


# --------------------------------------------------------------------------
# GET /api/v1/health/live  — unauthenticated liveness probe (k8s, LB)
# GET /api/v1/health       — full readiness probe (auth + RPC)
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# POST /api/v1/alert/test  — send a synthetic Telegram alert (1.9.0+)
# --------------------------------------------------------------------------

@router.post("/alert/test")
def alert_test_endpoint(
    request: Request,
    _: str = Depends(require_scope("admin")),
):
    """Send a synthetic alert to the configured Telegram chat.

    Used by the UI's Notifications tab "Send test" button and by
    operators verifying their bot wiring without waiting for a real
    event. Always tagged ``synthetic=true`` so receivers can ignore.

    Returns 200 if alerts are configured + the test was enqueued;
    503 with a clear message otherwise. Does NOT wait for delivery —
    the worker picks it up on its next tick.
    """
    from skr_crypto.server import alerts as alerts_mod
    if not alerts_mod.is_configured():
        return Response(
            content='{"error":"alerts not configured","code":"ALERT_DISABLED"}',
            media_type="application/json",
            status_code=503,
        )
    from datetime import UTC, datetime
    fake_entry = {
        "id": "synthetic-test",
        "event": "ALERT_TEST",
        "timestamp": datetime.now(UTC).isoformat(),
        "wallet": "test",
        "from_address": "TTestxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "to_address": "TXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "amount": "1.00",
        "asset": "USDT",
        "txid": "synthetic-test-tx-id",
        "idempotency_key": "synthetic-key",
        "client_ip": client_address(request),
        "token_id": "synthetic",
        "result": "test",
        "details": "synthetic test alert from POST /api/v1/alert/test",
    }
    message = alerts_mod.format_message(fake_entry)
    # Bypass matches_rule — operator explicitly asked for a test.
    try:
        alerts_mod.store.enqueue(  # type: ignore[union-attr]
            audit_event_id=fake_entry["id"],
            event=fake_entry["event"],
            chat_id=alerts_mod._chat_id,
            message=message,
        )
    except Exception as exc:
        return Response(
            content=f'{{"error":"enqueue failed: {exc}","code":"ALERT_ENQUEUE_FAILED"}}',
            media_type="application/json",
            status_code=500,
        )
    return {"status": "queued", "synthetic": True}


# --------------------------------------------------------------------------
# GET /api/v1/alert/config  — read-only view of alert configuration (1.9.0+)
# --------------------------------------------------------------------------

@router.get("/alert/config")
def alert_config_endpoint(_: str = Depends(require_scope("read"))):
    """Return the current alert configuration (no secrets).

    Operator UI uses this to render the Notifications tab. The bot
    token is NEVER returned — only whether one is configured.
    """
    from skr_crypto.server import alerts as alerts_mod
    from skr_crypto.server.config import (
        ALERT_EVENTS,
        ALERT_QUIET_HOURS_UTC,
        ALERT_SANCTIONS_HIT_NOTIFY,
        ALERT_SEND_THRESHOLD_USDT,
    )
    return {
        "configured": alerts_mod.is_configured(),
        "chat_id": alerts_mod._chat_id if alerts_mod.is_configured() else "",
        "events": list(ALERT_EVENTS),
        "send_threshold_usdt": (
            str(ALERT_SEND_THRESHOLD_USDT) if ALERT_SEND_THRESHOLD_USDT else None
        ),
        "sanctions_hit_notify": ALERT_SANCTIONS_HIT_NOTIFY,
        "quiet_hours_utc": ALERT_QUIET_HOURS_UTC,
    }


# --------------------------------------------------------------------------
# GET /api/v1/reports/period             — period summary CSV/XLSX (1.9.0+)
# GET /api/v1/reports/sanctions-hits     — sanctions hits CSV/XLSX (1.9.0+)
# GET /api/v1/reports/period/sparkline   — JSON for the UI sparkline (1.9.0+)
# --------------------------------------------------------------------------

def _parse_iso_date(value: str, *, label: str):
    from datetime import date as _date
    try:
        return _date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label}={value!r} is not a YYYY-MM-DD date") from exc


@router.get("/reports/period")
def reports_period_endpoint(
    request: Request,
    from_: str = Query(..., alias="from", max_length=10,
                       description="Inclusive UTC start date YYYY-MM-DD"),
    to: str = Query(..., max_length=10,
                    description="Inclusive UTC end date YYYY-MM-DD"),
    format: str = Query(default="csv", pattern="^(csv|xlsx)$"),
    _: str = Depends(require_scope("read")),
):
    """Period summary report. CSV by default; XLSX requires [reports] extra."""
    from skr_crypto.server import reporting
    from skr_crypto.server.config import AUDIT_LOG_FILE
    try:
        from_date = _parse_iso_date(from_, label="from")
        to_date = _parse_iso_date(to, label="to")
        reporting.validate_range(from_date, to_date)
    except ValueError as exc:
        return Response(
            content=f'{{"error":"{exc}","code":"REPORT_BAD_RANGE"}}',
            media_type="application/json",
            status_code=400,
        )
    summary = reporting.aggregate_period(
        AUDIT_LOG_FILE or None, from_date=from_date, to_date=to_date,
    )
    filename = f"skr-crypto-period-{from_date}-to-{to_date}"
    if format == "xlsx":
        try:
            data = reporting.render_period_xlsx(summary)
        except reporting.XlsxExtraMissing as exc:
            return Response(
                content=f'{{"error":"{exc}","code":"REPORT_XLSX_EXTRA_MISSING"}}',
                media_type="application/json",
                status_code=503,
            )
        return Response(
            content=data,
            media_type=reporting.XLSX_CONTENT_TYPE,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}.xlsx"',
            },
        )
    csv_text = reporting.render_period_csv(summary)
    return Response(
        content=csv_text,
        media_type=reporting.CSV_CONTENT_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.csv"',
        },
    )


@router.get("/reports/sanctions-hits")
def reports_sanctions_endpoint(
    request: Request,
    from_: str = Query(..., alias="from", max_length=10),
    to: str = Query(..., max_length=10),
    format: str = Query(default="csv", pattern="^(csv|xlsx)$"),
    _: str = Depends(require_scope("read")),
):
    """Every SEND_REJECTED with a sanctions match in the date range."""
    from skr_crypto.server import reporting
    from skr_crypto.server.config import AUDIT_LOG_FILE
    try:
        from_date = _parse_iso_date(from_, label="from")
        to_date = _parse_iso_date(to, label="to")
        reporting.validate_range(from_date, to_date)
    except ValueError as exc:
        return Response(
            content=f'{{"error":"{exc}","code":"REPORT_BAD_RANGE"}}',
            media_type="application/json",
            status_code=400,
        )
    hits = reporting.list_sanctions_hits(
        AUDIT_LOG_FILE or None, from_date=from_date, to_date=to_date,
    )
    filename = f"skr-crypto-sanctions-{from_date}-to-{to_date}"
    if format == "xlsx":
        try:
            data = reporting.render_sanctions_xlsx(hits)
        except reporting.XlsxExtraMissing as exc:
            return Response(
                content=f'{{"error":"{exc}","code":"REPORT_XLSX_EXTRA_MISSING"}}',
                media_type="application/json",
                status_code=503,
            )
        return Response(
            content=data,
            media_type=reporting.XLSX_CONTENT_TYPE,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}.xlsx"',
            },
        )
    csv_text = reporting.render_sanctions_csv(hits)
    return Response(
        content=csv_text,
        media_type=reporting.CSV_CONTENT_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}.csv"',
        },
    )


@router.get("/reports/period/sparkline")
def reports_period_sparkline(
    request: Request,
    from_: str = Query(..., alias="from", max_length=10),
    to: str = Query(..., max_length=10),
    _: str = Depends(require_scope("read")),
):
    """JSON time series the UI plots as inline-SVG sparklines."""
    from skr_crypto.server import reporting
    from skr_crypto.server.config import AUDIT_LOG_FILE
    try:
        from_date = _parse_iso_date(from_, label="from")
        to_date = _parse_iso_date(to, label="to")
        reporting.validate_range(from_date, to_date)
    except ValueError as exc:
        return Response(
            content=f'{{"error":"{exc}","code":"REPORT_BAD_RANGE"}}',
            media_type="application/json",
            status_code=400,
        )
    summary = reporting.aggregate_period(
        AUDIT_LOG_FILE or None, from_date=from_date, to_date=to_date,
    )
    return reporting.sparkline_series(summary)


@router.get("/metrics")
def metrics_endpoint(_: str = Depends(require_scope("metrics"))):
    """Prometheus text exposition.

    Requires the same X-API-Key as every other endpoint — scrapers must
    carry the token. This avoids leaking balances and traffic patterns to
    anyone who can reach the service on the network. The scrape itself
    does NO RPC calls; gauges are refreshed by the normal /balance,
    /health, and /send paths.
    """
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
def health(request: Request, _: str = Depends(require_scope("read"))):
    client_ip = client_address(request)
    log.info("[HEALTH] Request from %s", client_ip)

    node_ok = tron.check_connection()
    metrics.node_connected_gauge.set(1.0 if node_ok else 0.0)

    return HealthResponse(
        network=TRON_NETWORK,
        uptime_seconds=uptime(),
        shutdown_in_seconds=seconds_remaining(),
        node_connected=node_ok,
        wallet_count=wallets.count(),
        wallet_names=wallets.names(),
    )
