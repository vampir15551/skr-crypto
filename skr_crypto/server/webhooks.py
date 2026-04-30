"""Opt-in HTTP webhook delivery for audit events. See ADR 0011.

Architecture in one diagram:

    audit.record(...)  ────► AuditWriter (fsync to audit.log)
            │
            └─► (if WEBHOOK_URLS set & event matches WEBHOOK_EVENTS)
                  ├─► enqueue(event_payload, url)  →  webhook_deliveries (SQLite)
                  └─► (worker thread, separate from request path)
                          ├─► HMAC-sign payload with WEBHOOK_SIGNING_SECRET
                          ├─► POST to URL
                          ├─► on 2xx → status='delivered'
                          ├─► on non-2xx / network err → bump attempts +
                          │                                schedule retry
                          └─► attempts > len(BACKOFF) → status='giving_up' +
                                                       WEBHOOK_GIVEUP audit event

Critical invariants:

  - The worker runs on a daemon thread, NOT on the request path. A slow
    receiver cannot block /send. Tested.
  - The worker NEVER calls back into wallet.send_usdt or modifies
    idempotency. Tested.
  - HMAC signing uses ``hmac.compare_digest``-friendly hex format. Receivers
    MUST verify both the signature AND the timestamp tolerance.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass

import requests

from skr_crypto.server import audit

log = logging.getLogger("payouts")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_event_id     TEXT NOT NULL,
    event              TEXT NOT NULL,
    url                TEXT NOT NULL,
    payload            TEXT NOT NULL,
    status             TEXT NOT NULL,
    attempts           INTEGER NOT NULL DEFAULT 0,
    next_attempt_at    REAL NOT NULL,
    last_response_at   REAL,
    last_response_code INTEGER,
    last_error         TEXT,
    created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wh_status_next ON webhook_deliveries(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_wh_audit ON webhook_deliveries(audit_event_id);
"""


_STATUS_PENDING = "pending"
_STATUS_DELIVERED = "delivered"
_STATUS_FAILED = "failed"
_STATUS_GIVING_UP = "giving_up"


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass
class WebhookDelivery:
    id: int
    audit_event_id: str
    event: str
    url: str
    payload: str
    status: str
    attempts: int
    next_attempt_at: float
    last_response_at: float | None
    last_response_code: int | None
    last_error: str | None
    created_at: float


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def sign_payload(secret: str, timestamp: int, body: str) -> str:
    """Return the v1 signature value for the X-SKR-Signature header.

    Format: ``t=<ts>,v1=<hex>`` where ``<hex>`` is HMAC-SHA256 of
    ``f"{ts}.{body}"`` keyed by ``secret``.
    """
    msg = f"{timestamp}.{body}".encode()
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


def verify_signature(
    secret: str, header: str, body: str, *,
    tolerance_sec: int = 300,
) -> bool:
    """Verify an X-SKR-Signature header. Returns True only if both:

      1. The HMAC-SHA256 matches (constant-time compare).
      2. The timestamp is within ``tolerance_sec`` of now.

    Used by the test harness; production receivers should re-implement
    this in their own language for clarity (5-line spec).
    """
    if not secret or not header:
        return False
    parts = {}
    for p in header.split(","):
        if "=" in p:
            k, v = p.split("=", 1)
            parts[k.strip()] = v.strip()
    if "t" not in parts or "v1" not in parts:
        return False
    try:
        ts = int(parts["t"])
    except ValueError:
        return False
    if abs(time.time() - ts) > tolerance_sec:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.{body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])


# ---------------------------------------------------------------------------
# Delivery store + worker
# ---------------------------------------------------------------------------


class WebhookDeliveryStore:
    """SQLite-backed store of webhook deliveries.

    In-memory mode (db_path=None) is for tests. The store is **not**
    a queue per se — it's a state table; the worker thread polls it
    on a fixed interval looking for pending rows whose
    ``next_attempt_at <= now()``.
    """

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path
        self._lock = threading.RLock()
        self._memory: dict[int, dict] = {}
        self._next_id = 1
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

    def enqueue(
        self, *, audit_event_id: str, event: str, url: str, payload: str,
    ) -> int:
        now = time.time()
        if self._conn is not None:
            cur = self._conn.execute(
                "INSERT INTO webhook_deliveries "
                "(audit_event_id, event, url, payload, status, attempts, "
                " next_attempt_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (audit_event_id, event, url, payload, _STATUS_PENDING, now, now),
            )
            return cur.lastrowid or 0
        with self._lock:
            i = self._next_id
            self._next_id += 1
            self._memory[i] = {
                "id": i,
                "audit_event_id": audit_event_id,
                "event": event,
                "url": url,
                "payload": payload,
                "status": _STATUS_PENDING,
                "attempts": 0,
                "next_attempt_at": now,
                "last_response_at": None,
                "last_response_code": None,
                "last_error": None,
                "created_at": now,
            }
            return i

    def take_due(self, limit: int = 50) -> list[WebhookDelivery]:
        """Return up to ``limit`` deliveries whose next_attempt_at is past."""
        now = time.time()
        if self._conn is not None:
            cur = self._conn.execute(
                "SELECT id, audit_event_id, event, url, payload, status, "
                "attempts, next_attempt_at, last_response_at, "
                "last_response_code, last_error, created_at "
                "FROM webhook_deliveries "
                "WHERE status=? AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at ASC LIMIT ?",
                (_STATUS_PENDING, now, limit),
            )
            return [WebhookDelivery(*row) for row in cur.fetchall()]
        with self._lock:
            rows = [
                d for d in self._memory.values()
                if d["status"] == _STATUS_PENDING and d["next_attempt_at"] <= now
            ]
            rows.sort(key=lambda d: d["next_attempt_at"])
            return [self._row_to_dataclass(d) for d in rows[:limit]]

    def update_outcome(
        self, delivery_id: int, *, status: str, attempts: int,
        next_attempt_at: float, last_response_at: float | None,
        last_response_code: int | None, last_error: str | None,
    ) -> None:
        if self._conn is not None:
            self._conn.execute(
                "UPDATE webhook_deliveries SET "
                "status=?, attempts=?, next_attempt_at=?, "
                "last_response_at=?, last_response_code=?, last_error=? "
                "WHERE id=?",
                (status, attempts, next_attempt_at, last_response_at,
                 last_response_code, last_error, delivery_id),
            )
            return
        with self._lock:
            d = self._memory.get(delivery_id)
            if d is None:
                return
            d.update({
                "status": status, "attempts": attempts,
                "next_attempt_at": next_attempt_at,
                "last_response_at": last_response_at,
                "last_response_code": last_response_code,
                "last_error": last_error,
            })

    def list_recent(self, *, limit: int = 100) -> list[WebhookDelivery]:
        if self._conn is not None:
            cur = self._conn.execute(
                "SELECT id, audit_event_id, event, url, payload, status, "
                "attempts, next_attempt_at, last_response_at, "
                "last_response_code, last_error, created_at "
                "FROM webhook_deliveries "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [WebhookDelivery(*row) for row in cur.fetchall()]
        with self._lock:
            rows = sorted(
                self._memory.values(),
                key=lambda d: d["created_at"], reverse=True,
            )[:limit]
            return [self._row_to_dataclass(d) for d in rows]

    def get(self, delivery_id: int) -> WebhookDelivery | None:
        if self._conn is not None:
            cur = self._conn.execute(
                "SELECT id, audit_event_id, event, url, payload, status, "
                "attempts, next_attempt_at, last_response_at, "
                "last_response_code, last_error, created_at "
                "FROM webhook_deliveries WHERE id=?",
                (delivery_id,),
            )
            row = cur.fetchone()
            return WebhookDelivery(*row) if row else None
        with self._lock:
            d = self._memory.get(delivery_id)
            return self._row_to_dataclass(d) if d else None

    def force_retry(self, delivery_id: int) -> bool:
        """Reset a giving_up / failed delivery back to pending for one
        more attempt. Returns True if the delivery was reset."""
        rec = self.get(delivery_id)
        if rec is None:
            return False
        if rec.status not in (_STATUS_GIVING_UP, _STATUS_FAILED):
            return False
        self.update_outcome(
            delivery_id,
            status=_STATUS_PENDING,
            attempts=rec.attempts,        # don't reset; counts cumulative
            next_attempt_at=time.time(),
            last_response_at=rec.last_response_at,
            last_response_code=rec.last_response_code,
            last_error=rec.last_error,
        )
        return True

    def _row_to_dataclass(self, d: dict) -> WebhookDelivery:
        return WebhookDelivery(
            id=d["id"], audit_event_id=d["audit_event_id"],
            event=d["event"], url=d["url"], payload=d["payload"],
            status=d["status"], attempts=d["attempts"],
            next_attempt_at=d["next_attempt_at"],
            last_response_at=d.get("last_response_at"),
            last_response_code=d.get("last_response_code"),
            last_error=d.get("last_error"),
            created_at=d["created_at"],
        )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._memory.clear()
            self._next_id = 1
            if self._conn is not None:
                self._conn.execute("DELETE FROM webhook_deliveries")


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------


class WebhookWorker(threading.Thread):
    """Daemon thread that pulls due deliveries and POSTs them.

    The worker is the ONLY component that does outbound HTTP for
    webhooks. /send and friends never block on a webhook.
    """

    def __init__(
        self,
        store: WebhookDeliveryStore,
        *,
        signing_secret: str,
        backoff_schedule: tuple[float, ...],
        timeout_sec: float,
        interval_sec: float,
        session: requests.Session | None = None,
    ) -> None:
        super().__init__(name="webhook-worker", daemon=True)
        self._store = store
        self._secret = signing_secret
        self._backoff = backoff_schedule
        self._timeout = timeout_sec
        self._interval = interval_sec
        self._stop = threading.Event()
        self._session = session or requests.Session()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                due = self._store.take_due(limit=50)
                for d in due:
                    if self._stop.is_set():
                        break
                    self._deliver_one(d)
            except Exception as exc:
                log.exception("[WEBHOOK] worker tick failed: %s", exc)
            self._stop.wait(self._interval)

    def _deliver_one(self, d: WebhookDelivery) -> None:
        ts = int(time.time())
        signature = sign_payload(self._secret, ts, d.payload)
        headers = {
            "Content-Type": "application/json",
            "X-SKR-Signature": signature,
            "X-SKR-Event": d.event,
            "X-SKR-Delivery-Id": str(d.id),
            "User-Agent": "skr-crypto-webhook/1",
        }
        attempts = d.attempts + 1
        try:
            resp = self._session.post(
                d.url, data=d.payload, headers=headers,
                timeout=self._timeout,
            )
            code = resp.status_code
            if 200 <= code < 300:
                self._store.update_outcome(
                    d.id, status=_STATUS_DELIVERED, attempts=attempts,
                    next_attempt_at=0.0, last_response_at=time.time(),
                    last_response_code=code, last_error=None,
                )
                log.info(
                    "[WEBHOOK] delivered id=%d event=%s url=%s code=%d "
                    "attempts=%d",
                    d.id, d.event, d.url, code, attempts,
                )
                return
            error = f"HTTP {code}: {resp.text[:200]}"
        except Exception as exc:
            code = None
            error = f"{type(exc).__name__}: {exc}"

        # Failure path — schedule next attempt or give up.
        self._handle_failure(d, attempts, code, error)

    def _handle_failure(
        self, d: WebhookDelivery, attempts: int,
        code: int | None, error: str,
    ) -> None:
        if attempts >= len(self._backoff):
            self._store.update_outcome(
                d.id, status=_STATUS_GIVING_UP, attempts=attempts,
                next_attempt_at=0.0, last_response_at=time.time(),
                last_response_code=code, last_error=error,
            )
            log.warning(
                "[WEBHOOK] GIVEUP id=%d event=%s url=%s attempts=%d err=%s",
                d.id, d.event, d.url, attempts, error,
            )
            try:
                audit.record(
                    "WEBHOOK_GIVEUP",
                    result="giveup",
                    details=(
                        f"delivery_id={d.id} event={d.event} url={d.url} "
                        f"attempts={attempts} last_code={code} err={error}"
                    ),
                )
            except Exception as exc:
                log.error("[WEBHOOK] audit GIVEUP write failed: %s", exc)
            return

        # Schedule retry per backoff schedule.
        next_in = self._backoff[attempts]  # 0-indexed; attempts=1 → backoff[1]
        self._store.update_outcome(
            d.id, status=_STATUS_PENDING, attempts=attempts,
            next_attempt_at=time.time() + next_in,
            last_response_at=time.time(),
            last_response_code=code, last_error=error,
        )
        log.info(
            "[WEBHOOK] retry id=%d event=%s attempts=%d in=%.0fs err=%s",
            d.id, d.event, attempts, next_in, error,
        )


# ---------------------------------------------------------------------------
# Module-level singleton (initialised by lifespan)
# ---------------------------------------------------------------------------


store: WebhookDeliveryStore | None = None
_worker: WebhookWorker | None = None
_event_filter: tuple[str, ...] = ()
_target_urls: tuple[str, ...] = ()


def init_webhooks(
    *,
    db_path: str | None,
    urls: tuple[str, ...],
    signing_secret: str,
    event_filter: tuple[str, ...],
    backoff_schedule: tuple[float, ...],
    timeout_sec: float,
    interval_sec: float,
) -> WebhookDeliveryStore | None:
    """Initialise the webhook subsystem if any URLs are configured.

    Returns the store on success, or None if webhooks are disabled
    (no URLs). Idempotent."""
    global store, _worker, _event_filter, _target_urls

    if _worker is not None:
        _worker.stop()
        _worker = None
    if store is not None:
        try:
            store.close()
        except Exception:
            pass
        store = None

    if not urls:
        log.info("[WEBHOOK] disabled (no WEBHOOK_URLS configured)")
        return None
    if not signing_secret:
        log.error(
            "[WEBHOOK] WEBHOOK_URLS set but WEBHOOK_SIGNING_SECRET empty — "
            "refusing to send unsigned deliveries. Generate one with "
            "`skr-crypto webhook setup`."
        )
        return None

    store = WebhookDeliveryStore(db_path=db_path)
    _event_filter = event_filter
    _target_urls = urls
    _worker = WebhookWorker(
        store, signing_secret=signing_secret,
        backoff_schedule=backoff_schedule, timeout_sec=timeout_sec,
        interval_sec=interval_sec,
    )
    _worker.start()
    log.info(
        "[WEBHOOK] initialised: %d URL(s), events=%s, backoff=%s",
        len(urls), event_filter, backoff_schedule,
    )
    return store


def shutdown_webhooks() -> None:
    global store, _worker
    if _worker is not None:
        _worker.stop()
        _worker = None
    if store is not None:
        try:
            store.close()
        except Exception:
            pass
        store = None


def enqueue_event_for_delivery(
    audit_event_id: str, event_name: str, payload_dict: dict,
) -> None:
    """Called by audit.record after the audit fsync lands.

    For each URL in WEBHOOK_URLS, INSERT a `pending` row. The worker
    picks it up on its next tick. Synchronous, fast — only a SQLite
    INSERT per URL.

    Errors are swallowed — the audit record is the source of truth;
    a failed enqueue is logged but never raises (we don't 5xx the
    /send caller because of webhook plumbing).
    """
    if store is None or not _target_urls:
        return
    if event_name not in _event_filter:
        return
    try:
        body = json.dumps(payload_dict, ensure_ascii=False)
    except Exception as exc:
        log.error("[WEBHOOK] payload serialisation failed: %s", exc)
        return
    for url in _target_urls:
        try:
            store.enqueue(
                audit_event_id=audit_event_id, event=event_name,
                url=url, payload=body,
            )
        except Exception as exc:
            log.error("[WEBHOOK] enqueue failed (url=%s): %s", url, exc)
