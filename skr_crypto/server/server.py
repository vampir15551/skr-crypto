from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from skr_crypto.server import metrics
from skr_crypto.server.audit import AuditWriteError
from skr_crypto.server.config import RATE_LIMIT_MAX, RATE_LIMIT_WINDOW
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
from skr_crypto.server.idempotency import IdempotencyConflict, UnresolvedIdempotency
from skr_crypto.server.net import client_address
from skr_crypto.server.routes import router
from skr_crypto.server.security import load_wallets, lock_key_store
from skr_crypto.server.shutdown import mark_started, reset_timer
from skr_crypto.server.tokens import init_token_store
from skr_crypto.server.tron_client import tron
from skr_crypto.server.wallet_pool import wallets

log = logging.getLogger("payouts")


# ---------------------------------------------------------------------------
# Rate limiter (in-memory, per-IP, thread-safe)
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            window = self._hits[key]
            self._hits[key] = [t for t in window if now - t < RATE_LIMIT_WINDOW]
            if len(self._hits[key]) >= RATE_LIMIT_MAX:
                return False
            self._hits[key].append(now)
            return True


_limiter = _RateLimiter()


# ---------------------------------------------------------------------------
# Lifespan (async wrapper required by ASGI, internals are sync)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # All sync calls — no await
    tron.init()
    # Initialise the per-caller token store. Shares the idempotency DB
    # path when configured; in-memory otherwise (dev / tests).
    from skr_crypto.server.config import IDEMPOTENCY_DB_PATH
    init_token_store(IDEMPOTENCY_DB_PATH or None)
    # Load every configured wallet through the active KeyProvider and
    # populate the pool BEFORE we start serving traffic. The
    # log line in WalletPool.init() prints the names so the operator
    # can confirm "yes, these are the wallets I expected."
    wallets.init(load_wallets())
    mark_started()
    reset_timer()
    # Refresh the OFAC SDN sanctions list (best-effort). The list is
    # cached in memory for the process lifetime; refresh happens on
    # next process start. If the network is down or the URL changed,
    # we still come up — the sanctions check just SKIPs until next
    # successful refresh.
    try:
        from skr_crypto.server import sanctions
        sanctions.load()
    except Exception as exc:
        log.warning("Sanctions list load failed: %s", exc)
    # Reconcile prior runs' SEND_SUCCESS records with on-chain state and
    # flag same-day duplicate recipients before accepting traffic.
    # Best-effort — never blocks startup. Imported lazily so an import-time
    # error in startup_check can't break service boot.
    try:
        from skr_crypto.server.startup_check import run_startup_check
        run_startup_check()
    except Exception as exc:
        log.warning("Startup check failed: %s", exc)
    log.info("Payout service ready")
    yield
    log.info("Payout service stopping")
    wallets.destroy()
    tron.destroy()
    # Close the SQLite idempotency store (if that's the backend) so any
    # outstanding WAL is flushed. In-memory store's close() is a no-op.
    from skr_crypto.server.idempotency import idempotency as _idem
    _idem.close()
    # Close the durable audit file BEFORE locking 1Password — if the disk
    # write fails, we still want to surface the error in stdout while we
    # have a logger.
    from skr_crypto.server.audit import close_audit_file
    close_audit_file()
    lock_key_store()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(
        title="USDT TRC-20 Payouts",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # ── Middleware (async required by ASGI, body is sync) ─────────────────

    @app.middleware("http")
    async def main_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:16])
        request.state.request_id = request_id

        # Don't rate-limit either health endpoint or /metrics — they're
        # polled by k8s/LB/Prometheus and the unauthenticated /health/live
        # in particular must always respond fast and cheap.
        path = request.url.path
        if path not in ("/api/v1/health", "/api/v1/health/live", "/api/v1/metrics"):
            client_ip = client_address(request)
            if not _limiter.check(client_ip):
                log.warning("[RATE_LIMIT] ip=%s path=%s", client_ip, path)
                metrics.rate_limit_drops_total.inc()
                metrics.http_requests_total.labels(
                    method=request.method, path=path, status="429"
                ).inc()
                return JSONResponse(
                    status_code=429,
                    content={"error": "Rate limit exceeded", "code": "RATE_LIMIT_EXCEEDED"},
                    headers={"X-Request-ID": request_id},
                )

        reset_timer()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        # Record the response class (2xx/3xx/4xx/5xx), not exact code, to
        # avoid label-cardinality explosions.
        status_class = f"{response.status_code // 100}xx"
        metrics.http_requests_total.labels(
            method=request.method, path=path, status=status_class,
        ).inc()
        return response

    # ── Exception handlers ───────────────────────────────────────────────

    @app.exception_handler(InvalidAddress)
    def handle_invalid_address(request: Request, exc: InvalidAddress):
        # Log the full address for forensics; the API response only includes
        # the truncated form via exc.message.
        log.warning("[ERROR] %s | %s | full_address=%s", exc.code, exc.message, exc.address)
        return JSONResponse(status_code=400, content={"error": exc.message, "code": exc.code})

    @app.exception_handler(InsufficientBalance)
    def handle_insufficient_balance(request: Request, exc: InsufficientBalance):
        log.warning("[ERROR] %s | %s", exc.code, exc.message)
        return JSONResponse(status_code=400, content={"error": exc.message, "code": exc.code})

    @app.exception_handler(EnergyTooExpensive)
    def handle_energy_too_expensive(request: Request, exc: EnergyTooExpensive):
        log.warning("[ERROR] %s | %s", exc.code, exc.message)
        return JSONResponse(
            status_code=400,
            content={
                "error": exc.message,
                "code": exc.code,
                "estimated_energy": exc.estimated_energy,
                "estimated_burn_trx": exc.estimated_burn_trx,
                "max_burn_trx": exc.max_burn_trx,
            },
        )

    @app.exception_handler(TransactionFailed)
    def handle_tx_failed(request: Request, exc: TransactionFailed):
        log.error("[ERROR] %s | %s", exc.code, exc.message)
        return JSONResponse(status_code=500, content={"error": "Transaction failed", "code": exc.code})

    @app.exception_handler(WalletNotFoundError)
    def handle_wallet_not_found(request: Request, exc: WalletNotFoundError):
        # 404: caller asked for a wallet name we don't know.
        log.warning("[ERROR] %s | name=%s available=%s", exc.code, exc.name, exc.available)
        return JSONResponse(
            status_code=404,
            content={
                "error": exc.message,
                "code": exc.code,
                "wallet": exc.name,
                "available": exc.available,
            },
        )

    @app.exception_handler(WalletPoolEmptyError)
    def handle_wallet_pool_empty(request: Request, exc: WalletPoolEmptyError):
        # 503: service is up but has nothing to sign with — operator must
        # configure at least one wallet and restart.
        log.error("[ERROR] %s | %s", exc.code, exc.message)
        return JSONResponse(
            status_code=503,
            content={"error": exc.message, "code": exc.code},
        )

    @app.exception_handler(WalletAutoPickFailed)
    def handle_wallet_autopick_failed(request: Request, exc: WalletAutoPickFailed):
        # 503: every balance lookup failed during auto-pick. Caller can
        # retry — pick an explicit wallet to bypass the lookup.
        log.error("[ERROR] %s | %s", exc.code, exc.message)
        return JSONResponse(
            status_code=503,
            content={"error": exc.message, "code": exc.code},
        )

    @app.exception_handler(RiskTooHigh)
    def handle_risk_too_high(request: Request, exc: RiskTooHigh):
        # Surface the full report so the caller can show the operator
        # exactly which checks fired without a second round trip to
        # /api/v1/risk. Status 400 — same class as the other "we
        # refused before broadcasting" gates (InsufficientBalance,
        # EnergyTooExpensive).
        log.warning(
            "[ERROR] %s | level=%s addr=%s",
            exc.code, exc.level, exc.report.get("address", "?"),
        )
        return JSONResponse(
            status_code=400,
            content={
                "error": exc.message,
                "code": exc.code,
                "level": exc.level,
                "report": exc.report,
            },
        )

    @app.exception_handler(IdempotencyConflict)
    def handle_idempotency_conflict(request: Request, exc: IdempotencyConflict):
        log.warning("[ERROR] IDEMPOTENCY_CONFLICT | %s", exc)
        metrics.idempotency_conflicts_total.inc()
        return JSONResponse(
            status_code=409,
            content={"error": str(exc), "code": "IDEMPOTENCY_CONFLICT"},
        )

    @app.exception_handler(UnresolvedIdempotency)
    def handle_idempotency_unresolved(request: Request, exc: UnresolvedIdempotency):
        # This is the dangerous one: a prior process crashed mid-broadcast
        # and we don't know whether the tx made it on-chain. We REFUSE to
        # silently retry. Operator must reconcile before sending again.
        log.error(
            "[ERROR] IDEMPOTENCY_UNRESOLVED | key=%s created_at=%s",
            exc.key, exc.created_at,
        )
        metrics.idempotency_unresolved_total.inc()
        return JSONResponse(
            status_code=409,
            content={
                "error": str(exc),
                "code": "IDEMPOTENCY_UNRESOLVED",
                "idempotency_key": exc.key,
                "created_at": exc.created_at,
            },
        )

    @app.exception_handler(AuditWriteError)
    def handle_audit_write_error(request: Request, exc: AuditWriteError):
        # Durable audit failed mid-request. We don't know whether the
        # broadcast happened (audit could fail before OR after send_usdt),
        # so the response must be 5xx and the operator must investigate
        # tronscan / logs before retrying.
        log.error("[ERROR] AUDIT_WRITE_FAILED | %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": str(exc), "code": "AUDIT_WRITE_FAILED"},
        )

    @app.exception_handler(PayoutError)
    def handle_payout_error(request: Request, exc: PayoutError):
        log.error("[ERROR] %s | %s", exc.code, exc.message)
        return JSONResponse(status_code=500, content={"error": exc.message, "code": exc.code})

    app.include_router(router)
    return app
