"""Opt-in Telegram operator alerts. See ADR 0013.

Architecture in one diagram:

    audit.record(...)  ────► AuditWriter (fsync to audit.log)
            │
            ├─► webhooks.enqueue_event_for_delivery (ADR 0011)
            │
            └─► (if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID set)
                  alerts.notify_event(audit_entry)
                    │
                    └─► matches a rule? (ALERT_EVENTS, threshold,
                    │                    sanctions hit)
                    │
                    └─► enqueue(chat_id, formatted_message)
                          → alert_deliveries (SQLite)
                          → (worker thread, separate from request path)
                              ├─► POST api.telegram.org/bot<TOKEN>/sendMessage
                              ├─► on 2xx → status='delivered'
                              ├─► on non-2xx / network err → bump attempts +
                              │                                schedule retry
                              └─► attempts > len(BACKOFF) → status='giving_up'

Critical invariants:

  - The worker runs on a daemon thread, NOT on the request path. A slow
    Telegram cannot block /send. Tested.
  - The worker NEVER calls back into wallet.send_usdt or modifies
    idempotency / audit. Tested.
  - The bot token is NEVER logged or echoed; it lives in env, gets
    interpolated into the URL, and the URL is logged with the token
    redacted. Tested.
  - Sanctions hits bypass quiet hours.
  - SEND_FAILED bypasses quiet hours.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import requests

from skr_crypto.server import metrics

log = logging.getLogger("payouts")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_deliveries (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_event_id     TEXT NOT NULL,
    event              TEXT NOT NULL,
    chat_id            TEXT NOT NULL,
    message            TEXT NOT NULL,
    status             TEXT NOT NULL,
    attempts           INTEGER NOT NULL DEFAULT 0,
    next_attempt_at    REAL NOT NULL,
    last_response_at   REAL,
    last_response_code INTEGER,
    last_error         TEXT,
    created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_al_status_next ON alert_deliveries(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_al_audit ON alert_deliveries(audit_event_id);
"""


_STATUS_PENDING = "pending"
_STATUS_DELIVERED = "delivered"
_STATUS_FAILED = "failed"
_STATUS_GIVING_UP = "giving_up"


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass
class AlertDelivery:
    id: int
    audit_event_id: str
    event: str
    chat_id: str
    message: str
    status: str
    attempts: int
    next_attempt_at: float
    last_response_at: float | None
    last_response_code: int | None
    last_error: str | None
    created_at: float


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------


def _is_sanctions_hit(entry: dict) -> bool:
    """A SEND_REJECTED whose details mention the sanctions check.

    The risk module emits a comma-separated `failed=...` list inside
    `details` (see routes._send_impl). We match the substring
    'sanctions' rather than parsing the list — the check name is
    stable enough that this is the simplest reliable signal.
    """
    if entry.get("event") != "SEND_REJECTED":
        return False
    details = (entry.get("details") or "").lower()
    return "sanctions" in details


def _meets_threshold(entry: dict, threshold: Decimal | None) -> bool:
    if threshold is None:
        return False
    if entry.get("event") != "SEND_SUCCESS":
        return False
    raw = entry.get("amount") or "0"
    try:
        return Decimal(str(raw)) >= threshold
    except (InvalidOperation, ValueError):
        return False


def _in_quiet_hours(spec: str, now_utc_hour: int) -> bool:
    """Return True if `now_utc_hour` falls inside the quiet window.

    Spec format: "HH-HH" (e.g. "22-08"). Wraparound supported.
    Empty / malformed spec → False (no quiet hours).
    """
    if not spec or "-" not in spec:
        return False
    try:
        start_s, end_s = spec.split("-", 1)
        start = int(start_s)
        end = int(end_s)
    except ValueError:
        return False
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return False
    if start == end:
        return False
    if start < end:
        return start <= now_utc_hour < end
    # Wraparound: e.g. 22-08 = 22:00..23:59 OR 00:00..07:59
    return now_utc_hour >= start or now_utc_hour < end


def _bypasses_quiet_hours(entry: dict) -> bool:
    """Critical events that should always punch through quiet hours."""
    event = entry.get("event")
    if event == "SEND_FAILED":
        return True
    if _is_sanctions_hit(entry):
        return True
    return False


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _short(value: str | None, max_len: int = 200) -> str:
    s = (value or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _esc(value: str | None) -> str:
    """Minimal HTML escape for Telegram parse_mode=HTML.

    Telegram's HTML mode requires &, <, > to be escaped. Anything else
    is literal. Keep this trivial — we control the templates.
    """
    s = "" if value is None else str(value)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_message(entry: dict, *, threshold: Decimal | None = None) -> str:
    """Build the Telegram message text for an audit entry.

    Returns HTML-formatted text suitable for parse_mode=HTML.
    Caller has already decided this entry should alert; this function
    only formats.
    """
    event = entry.get("event") or "?"
    sanctions = _is_sanctions_hit(entry)
    high_value = _meets_threshold(entry, threshold)

    if sanctions:
        prefix = "🚨"
        title = "OFAC SANCTIONS REJECT"
    elif event == "SEND_FAILED":
        prefix = "❌"
        title = "SEND FAILED"
    elif event == "SEND_REJECTED":
        prefix = "⛔"
        title = "SEND REJECTED"
    elif event == "WEBHOOK_GIVEUP":
        prefix = "⚠️"
        title = "WEBHOOK GIVEUP"
    elif high_value:
        prefix = "💰"
        title = f"HIGH-VALUE SEND ≥ {threshold} USDT"
    else:
        prefix = "ℹ️"  # noqa: RUF001 — info emoji, not Latin "i"
        title = event

    lines = [f"{prefix} <b>{_esc(title)}</b>"]
    lines.append("")
    if entry.get("wallet"):
        lines.append(f"Wallet: <code>{_esc(entry['wallet'])}</code>")
    if entry.get("from_address"):
        lines.append(f"From:   <code>{_esc(entry['from_address'])}</code>")
    if entry.get("to_address"):
        lines.append(f"To:     <code>{_esc(entry['to_address'])}</code>")
    if entry.get("amount"):
        asset = entry.get("asset") or "USDT"
        lines.append(f"Amount: <b>{_esc(entry['amount'])} {_esc(asset)}</b>")
    if entry.get("txid"):
        lines.append(f"Txid:   <code>{_esc(entry['txid'])}</code>")
    if entry.get("idempotency_key"):
        lines.append(f"Key:    <code>{_esc(entry['idempotency_key'])}</code>")
    if entry.get("token_id"):
        lines.append(f"Token:  <code>{_esc(entry['token_id'])}</code>")
    if entry.get("result"):
        lines.append(f"Result: <code>{_esc(entry['result'])}</code>")
    if entry.get("details"):
        lines.append(f"Details: {_esc(_short(entry['details']))}")

    ts = entry.get("timestamp")
    if not ts:
        ts = datetime.now(UTC).isoformat()
    lines.append("")
    lines.append(f"<i>{_esc(ts)}</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Delivery store
# ---------------------------------------------------------------------------


class AlertDeliveryStore:
    """SQLite-backed store of Telegram alert deliveries.

    In-memory mode (``db_path=None``) is for tests. Mirrors the
    structure of ``WebhookDeliveryStore`` so the operator can read
    one and understand the other.
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
        self, *, audit_event_id: str, event: str, chat_id: str, message: str,
    ) -> int:
        now = time.time()
        if self._conn is not None:
            cur = self._conn.execute(
                "INSERT INTO alert_deliveries "
                "(audit_event_id, event, chat_id, message, status, attempts, "
                " next_attempt_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (audit_event_id, event, chat_id, message, _STATUS_PENDING, now, now),
            )
            return cur.lastrowid or 0
        with self._lock:
            i = self._next_id
            self._next_id += 1
            self._memory[i] = {
                "id": i, "audit_event_id": audit_event_id,
                "event": event, "chat_id": chat_id, "message": message,
                "status": _STATUS_PENDING, "attempts": 0,
                "next_attempt_at": now, "last_response_at": None,
                "last_response_code": None, "last_error": None,
                "created_at": now,
            }
            return i

    def take_due(self, limit: int = 50) -> list[AlertDelivery]:
        now = time.time()
        if self._conn is not None:
            cur = self._conn.execute(
                "SELECT id, audit_event_id, event, chat_id, message, status, "
                "attempts, next_attempt_at, last_response_at, "
                "last_response_code, last_error, created_at "
                "FROM alert_deliveries "
                "WHERE status=? AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at ASC LIMIT ?",
                (_STATUS_PENDING, now, limit),
            )
            return [AlertDelivery(*row) for row in cur.fetchall()]
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
                "UPDATE alert_deliveries SET "
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

    def list_recent(self, *, limit: int = 100) -> list[AlertDelivery]:
        if self._conn is not None:
            cur = self._conn.execute(
                "SELECT id, audit_event_id, event, chat_id, message, status, "
                "attempts, next_attempt_at, last_response_at, "
                "last_response_code, last_error, created_at "
                "FROM alert_deliveries "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [AlertDelivery(*row) for row in cur.fetchall()]
        with self._lock:
            rows = sorted(
                self._memory.values(),
                key=lambda d: d["created_at"], reverse=True,
            )[:limit]
            return [self._row_to_dataclass(d) for d in rows]

    def get(self, delivery_id: int) -> AlertDelivery | None:
        if self._conn is not None:
            cur = self._conn.execute(
                "SELECT id, audit_event_id, event, chat_id, message, status, "
                "attempts, next_attempt_at, last_response_at, "
                "last_response_code, last_error, created_at "
                "FROM alert_deliveries WHERE id=?",
                (delivery_id,),
            )
            row = cur.fetchone()
            return AlertDelivery(*row) if row else None
        with self._lock:
            d = self._memory.get(delivery_id)
            return self._row_to_dataclass(d) if d else None

    def force_retry(self, delivery_id: int) -> bool:
        rec = self.get(delivery_id)
        if rec is None:
            return False
        if rec.status not in (_STATUS_GIVING_UP, _STATUS_FAILED):
            return False
        self.update_outcome(
            delivery_id,
            status=_STATUS_PENDING,
            attempts=rec.attempts,
            next_attempt_at=time.time(),
            last_response_at=rec.last_response_at,
            last_response_code=rec.last_response_code,
            last_error=rec.last_error,
        )
        return True

    def _row_to_dataclass(self, d: dict) -> AlertDelivery:
        return AlertDelivery(
            id=d["id"], audit_event_id=d["audit_event_id"],
            event=d["event"], chat_id=d["chat_id"], message=d["message"],
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
                self._conn.execute("DELETE FROM alert_deliveries")


# ---------------------------------------------------------------------------
# Telegram transport
# ---------------------------------------------------------------------------


def _redact_token(url: str, token: str) -> str:
    """Replace the token in a Telegram API URL with '<redacted>'.

    We log delivery URLs for diagnostics but the URL embeds the bot
    token. Strip it before any log line.
    """
    if not token:
        return url
    return url.replace(token, "<redacted>")


def telegram_send_message(
    bot_token: str, chat_id: str, text: str, *,
    timeout_sec: float = 10.0, session: requests.Session | None = None,
) -> tuple[int, str]:
    """POST a sendMessage call to Telegram Bot API. Returns (status, body).

    Raises requests exceptions on network failure — the worker handles
    those as a delivery failure (retriable). 4xx / 5xx are returned as
    (status, body) for the worker to log + decide.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    sess = session or requests
    resp = sess.post(url, json=payload, timeout=timeout_sec)
    return resp.status_code, (resp.text or "")[:300]


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------


class AlertWorker(threading.Thread):
    """Daemon thread that pulls due deliveries and POSTs them to Telegram."""

    def __init__(
        self,
        store: AlertDeliveryStore,
        *,
        bot_token: str,
        backoff_schedule: tuple[float, ...],
        timeout_sec: float,
        interval_sec: float,
        session: requests.Session | None = None,
    ) -> None:
        super().__init__(name="alert-worker", daemon=True)
        self._store = store
        self._token = bot_token
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
                log.exception("[ALERT] worker tick failed: %s", exc)
            self._stop.wait(self._interval)

    def _deliver_one(self, d: AlertDelivery) -> None:
        attempts = d.attempts + 1
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        safe_url = _redact_token(url, self._token)
        try:
            code, body = telegram_send_message(
                self._token, d.chat_id, d.message,
                timeout_sec=self._timeout, session=self._session,
            )
            if 200 <= code < 300:
                self._store.update_outcome(
                    d.id, status=_STATUS_DELIVERED, attempts=attempts,
                    next_attempt_at=0.0, last_response_at=time.time(),
                    last_response_code=code, last_error=None,
                )
                metrics.alert_attempts_total.labels(result="success").inc()
                log.info(
                    "[ALERT] delivered id=%d event=%s chat=%s code=%d "
                    "attempts=%d url=%s",
                    d.id, d.event, d.chat_id, code, attempts, safe_url,
                )
                return
            error = f"HTTP {code}: {body}"
        except Exception as exc:
            code = None
            error = f"{type(exc).__name__}: {exc}"

        metrics.alert_attempts_total.labels(result="fail").inc()
        self._handle_failure(d, attempts, code, error, safe_url)

    def _handle_failure(
        self, d: AlertDelivery, attempts: int,
        code: int | None, error: str, safe_url: str,
    ) -> None:
        if attempts >= len(self._backoff):
            self._store.update_outcome(
                d.id, status=_STATUS_GIVING_UP, attempts=attempts,
                next_attempt_at=0.0, last_response_at=time.time(),
                last_response_code=code, last_error=error,
            )
            metrics.alert_giveup_total.inc()
            log.warning(
                "[ALERT] GIVEUP id=%d event=%s chat=%s attempts=%d url=%s err=%s",
                d.id, d.event, d.chat_id, attempts, safe_url, error,
            )
            return

        next_in = self._backoff[attempts]
        self._store.update_outcome(
            d.id, status=_STATUS_PENDING, attempts=attempts,
            next_attempt_at=time.time() + next_in,
            last_response_at=time.time(),
            last_response_code=code, last_error=error,
        )
        log.info(
            "[ALERT] retry id=%d event=%s attempts=%d in=%.0fs err=%s",
            d.id, d.event, attempts, next_in, error,
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


store: AlertDeliveryStore | None = None
_worker: AlertWorker | None = None
_chat_id: str = ""
_bot_token: str = ""
_event_filter: tuple[str, ...] = ()
_threshold: Decimal | None = None
_sanctions_notify: bool = True
_quiet_spec: str = ""


def init_alerts(
    *,
    db_path: str | None,
    bot_token: str,
    chat_id: str,
    event_filter: tuple[str, ...],
    send_threshold_usdt: Decimal | None,
    sanctions_notify: bool,
    quiet_hours_utc: str,
    backoff_schedule: tuple[float, ...],
    timeout_sec: float,
    interval_sec: float,
) -> AlertDeliveryStore | None:
    """Initialise the alert subsystem if Telegram is configured.

    Returns the store on success, or None if alerts are disabled
    (token or chat_id missing). Idempotent."""
    global store, _worker, _chat_id, _bot_token
    global _event_filter, _threshold, _sanctions_notify, _quiet_spec

    if _worker is not None:
        _worker.stop()
        _worker = None
    if store is not None:
        try:
            store.close()
        except Exception:
            pass
        store = None

    if not bot_token or not chat_id:
        log.info(
            "[ALERT] disabled (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "must both be set)"
        )
        _bot_token = ""
        _chat_id = ""
        return None

    store = AlertDeliveryStore(db_path=db_path)
    _bot_token = bot_token
    _chat_id = chat_id
    _event_filter = event_filter
    _threshold = send_threshold_usdt
    _sanctions_notify = sanctions_notify
    _quiet_spec = quiet_hours_utc
    _worker = AlertWorker(
        store, bot_token=bot_token,
        backoff_schedule=backoff_schedule, timeout_sec=timeout_sec,
        interval_sec=interval_sec,
    )
    _worker.start()
    log.info(
        "[ALERT] initialised: chat_id=%s events=%s threshold=%s "
        "quiet=%s backoff=%s",
        chat_id, event_filter,
        str(send_threshold_usdt) if send_threshold_usdt else "off",
        quiet_hours_utc or "off", backoff_schedule,
    )
    return store


def shutdown_alerts() -> None:
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


def is_configured() -> bool:
    return bool(_bot_token and _chat_id and store is not None)


def matches_rule(entry: dict) -> bool:
    """Return True if this audit entry triggers an alert."""
    if not is_configured():
        return False
    event = entry.get("event") or ""
    if event in _event_filter:
        return True
    if _meets_threshold(entry, _threshold):
        return True
    if _sanctions_notify and _is_sanctions_hit(entry):
        return True
    return False


def notify_event(entry: dict) -> None:
    """Called by audit.record after the audit fsync lands.

    Synchronous and fast — only a SQLite INSERT (or no-op). The
    worker thread does the actual HTTP. Errors are swallowed; the
    audit log is the source of truth, an alert enqueue failure is
    logged but never raises.
    """
    if not is_configured():
        return
    if not matches_rule(entry):
        return
    # Quiet hours: drop unless event is critical.
    if _quiet_spec:
        now_hour = datetime.now(UTC).hour
        if _in_quiet_hours(_quiet_spec, now_hour) and not _bypasses_quiet_hours(entry):
            log.debug(
                "[ALERT] suppressed (quiet hours %s) event=%s id=%s",
                _quiet_spec, entry.get("event"), entry.get("id"),
            )
            return
    try:
        message = format_message(entry, threshold=_threshold)
    except Exception as exc:
        log.error("[ALERT] format failed: %s", exc)
        return
    try:
        store.enqueue(
            audit_event_id=entry.get("id", ""),
            event=entry.get("event", ""),
            chat_id=_chat_id,
            message=message,
        )
    except Exception as exc:
        log.error("[ALERT] enqueue failed: %s", exc)
