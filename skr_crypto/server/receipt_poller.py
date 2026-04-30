"""Postmortem-only receipt poller — see ADR 0010.

Watches recently-broadcast transactions and resolves their on-chain
status in a `tx_status` table. Writes a `RECEIPT_RESOLVED` audit
event when a status becomes terminal.

Critical: this module **never** retries a broadcast. It is read-only
on the money path. The only writes it performs are to the
`tx_status` table and the audit log.

Lifecycle:

  - Background daemon thread, started by FastAPI lifespan after the
    wallet pool is initialised.
  - Polls the idempotency DB for COMMITTED keys whose txid hasn't
    been resolved yet.
  - For each, calls TronGrid `get_transaction_info(txid)` and maps
    the response to a status code.
  - Stops cleanly when ``stop()`` is called from the lifespan
    shutdown path.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass

from skr_crypto.server import audit
from skr_crypto.server.config import (
    RECEIPT_LOOKBACK_HOURS,
    RECEIPT_NOT_FOUND_GIVEUP_HOURS,
    RECEIPT_POLL_BATCH,
    RECEIPT_POLL_INTERVAL_SEC,
)

log = logging.getLogger("payouts")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Terminal status codes — once a tx_status row reaches one of these,
# we stop polling it and emit RECEIPT_RESOLVED.
TERMINAL_STATUSES: frozenset[str] = frozenset({
    "SUCCESS",
    "OUT_OF_ENERGY",
    "REVERT",
    "BAD_JUMP_DESTINATION",
    "OUT_OF_TIME",
    "STACK_TOO_SMALL",
    "STACK_TOO_LARGE",
    "ILLEGAL_OPERATION",
    "STACK_OVERFLOW",
    "JVM_STACK_OVER_FLOW",
    "TRANSFER_FAILED",
    "UNKNOWN",      # explicit upstream "we don't know" — terminal at our layer
    "GIVEUP",       # internal: NOT_FOUND for too long → operator signal
})

# Non-terminal codes we retry on next tick.
RETRY_STATUSES: frozenset[str] = frozenset({
    "NOT_FOUND",     # not yet in a block
    "RPC_ERROR",     # transient
    "PENDING",       # in mempool but not yet included
})


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@dataclass
class TxStatus:
    txid: str
    idempotency_key: str | None
    first_seen: float
    last_checked: float | None
    status: str | None
    block_number: int | None
    last_error: str | None
    resolved_at: float | None


# ---------------------------------------------------------------------------
# Status mapper
# ---------------------------------------------------------------------------


def _classify_receipt(info: dict | None, exc: Exception | None) -> tuple[str, int | None, str | None]:
    """Map ``get_transaction_info`` output to (status, block_number, error_msg).

    Returns one of TERMINAL_STATUSES or RETRY_STATUSES.
    """
    if exc is not None:
        msg = str(exc).lower()
        cls = type(exc).__name__.lower()
        if "not found" in msg or "transactionnotfound" in cls:
            return "NOT_FOUND", None, None
        return "RPC_ERROR", None, f"{type(exc).__name__}: {exc}"

    if not info:
        return "NOT_FOUND", None, None

    receipt = info.get("receipt") or {}
    block_number = info.get("blockNumber") or info.get("block_number")
    if block_number is not None:
        try:
            block_number = int(block_number)
        except (TypeError, ValueError):
            block_number = None

    # tronpy omits "result" entirely when the receipt is SUCCESS — only
    # failure codes are present. So absence of result + presence of
    # blockNumber means SUCCESS.
    explicit = receipt.get("result")
    if explicit and explicit not in ("SUCCESS",):
        return str(explicit).upper(), block_number, None

    if block_number is not None:
        return "SUCCESS", block_number, None

    # No receipt yet but tx exists in the response — pending in mempool.
    return "PENDING", None, None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tx_status (
    txid             TEXT PRIMARY KEY,
    idempotency_key  TEXT,
    first_seen       REAL NOT NULL,
    last_checked     REAL,
    status           TEXT,
    block_number     INTEGER,
    last_error       TEXT,
    resolved_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_tx_status_resolved ON tx_status(resolved_at);
CREATE INDEX IF NOT EXISTS idx_tx_status_idempotency_key ON tx_status(idempotency_key);
"""


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------


class ReceiptPoller:
    """Daemon-thread receipt poller. Lifecycle managed by FastAPI lifespan."""

    def __init__(
        self,
        db_path: str | None,
        tron_client,
        *,
        interval_sec: float | None = None,
        lookback_hours: float | None = None,
        batch_size: int | None = None,
        not_found_giveup_hours: float | None = None,
    ) -> None:
        self._db_path = db_path or None
        self._tron = tron_client
        self._interval = interval_sec or RECEIPT_POLL_INTERVAL_SEC
        self._lookback = lookback_hours or RECEIPT_LOOKBACK_HOURS
        self._batch = batch_size or RECEIPT_POLL_BATCH
        self._giveup = not_found_giveup_hours or RECEIPT_NOT_FOUND_GIVEUP_HOURS

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # In-memory mode: tests, dev. We keep a dict keyed by txid.
        self._memory: dict[str, dict] = {}

        # SQLite connection for tx_status. Reuses the idempotency DB
        # path; opens its own connection so it doesn't contend with
        # the idempotency store's writers on the same Connection
        # object.
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

    # ------ lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Begin the polling thread. Idempotent."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="receipt-poller", daemon=True,
            )
            self._thread.start()
        log.info(
            "[POLLER] started: interval=%.0fs lookback=%.0fh batch=%d giveup=%.0fh "
            "(db=%s)",
            self._interval, self._lookback, self._batch, self._giveup,
            self._db_path or "in-memory",
        )

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the poller to stop and wait briefly for the thread."""
        with self._lock:
            if self._thread is None:
                return
            self._stop_event.set()
            t = self._thread
        t.join(timeout=timeout)
        if t.is_alive():
            log.warning("[POLLER] thread did not stop within %.1fs", timeout)
        else:
            log.info("[POLLER] stopped cleanly")
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            self._thread = None

    # ------ main loop ------------------------------------------------------

    def _run(self) -> None:
        """Tick forever (until stop_event is set). Each tick polls a
        bounded batch of unresolved txids."""
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                # Never crash the daemon thread. Log and keep going.
                log.exception("[POLLER] tick failed: %s", exc)
            # Wake early on stop signal
            self._stop_event.wait(self._interval)

    def _tick(self) -> None:
        """Pick up to batch unresolved txids and probe each one."""
        candidates = self._next_candidates()
        if not candidates:
            return
        log.debug("[POLLER] tick: %d candidate(s)", len(candidates))
        for cand in candidates:
            if self._stop_event.is_set():
                return
            self._poll_one(cand)

    # ------ DB helpers -----------------------------------------------------

    def _next_candidates(self) -> list[dict]:
        """Return up to ``batch`` unresolved tx records.

        A candidate is:
          1. An idempotency entry committed within the lookback window,
          2. that has no terminal status in tx_status yet,
          3. preferring ones with the oldest last_checked (or never checked).
        """
        cutoff = time.time() - self._lookback * 3600.0
        if self._conn is not None:
            return self._next_candidates_sqlite(cutoff)
        return self._next_candidates_memory(cutoff)

    def _next_candidates_sqlite(self, cutoff: float) -> list[dict]:
        # Join idempotency.committed entries with tx_status; pick rows
        # that are not yet terminal.
        cur = self._conn.execute(
            """
            SELECT i.key, i.txid, i.created_at,
                   s.first_seen, s.last_checked, s.status, s.resolved_at
            FROM idempotency i
            LEFT JOIN tx_status s ON s.txid = i.txid
            WHERE i.status = 'committed'
              AND i.created_at >= ?
              AND (s.resolved_at IS NULL)
            ORDER BY COALESCE(s.last_checked, 0) ASC
            LIMIT ?
            """,
            (cutoff, self._batch),
        )
        rows = cur.fetchall()
        return [
            {
                "txid": r[1],
                "idempotency_key": r[0],
                "committed_at": r[2],
                "first_seen": r[3],
                "last_checked": r[4],
                "status": r[5],
            }
            for r in rows
        ]

    def _next_candidates_memory(self, cutoff: float) -> list[dict]:
        """In-memory mode is used only by tests; the test harness
        seeds the dict directly."""
        # Sort by last_checked (None first), pick batch
        rows = sorted(
            (r for r in self._memory.values() if r.get("resolved_at") is None),
            key=lambda r: r.get("last_checked") or 0.0,
        )
        return rows[: self._batch]

    def _upsert(self, rec: dict) -> None:
        """Insert or update a tx_status row."""
        if self._conn is not None:
            self._conn.execute(
                """
                INSERT INTO tx_status
                  (txid, idempotency_key, first_seen, last_checked, status,
                   block_number, last_error, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(txid) DO UPDATE SET
                  last_checked=excluded.last_checked,
                  status=excluded.status,
                  block_number=excluded.block_number,
                  last_error=excluded.last_error,
                  resolved_at=excluded.resolved_at
                """,
                (
                    rec["txid"], rec.get("idempotency_key"),
                    rec["first_seen"], rec.get("last_checked"),
                    rec.get("status"),
                    rec.get("block_number"), rec.get("last_error"),
                    rec.get("resolved_at"),
                ),
            )
        else:
            self._memory[rec["txid"]] = dict(rec)

    # ------ poll a single txid --------------------------------------------

    def _poll_one(self, cand: dict) -> None:
        """RPC + classify + UPSERT + audit-on-terminal."""
        txid = cand["txid"]
        now = time.time()
        first_seen = cand.get("first_seen") or now
        info = None
        exc = None
        try:
            info = self._tron.client.get_transaction_info(txid)
        except Exception as e:
            exc = e

        status, block, error = _classify_receipt(info, exc)

        # NOT_FOUND beyond the give-up threshold → terminal GIVEUP
        if status == "NOT_FOUND":
            age_hours = (now - first_seen) / 3600.0
            if age_hours >= self._giveup:
                status = "GIVEUP"
                error = (
                    f"NOT_FOUND for {age_hours:.1f}h (>{self._giveup}h giveup); "
                    "tx may have been mempool-evicted"
                )

        is_terminal = status in TERMINAL_STATUSES
        rec = {
            "txid": txid,
            "idempotency_key": cand.get("idempotency_key"),
            "first_seen": first_seen,
            "last_checked": now,
            "status": status if is_terminal else None,
            "block_number": block,
            "last_error": error,
            "resolved_at": now if is_terminal else None,
        }
        # Always persist the touch — even non-terminal ticks update last_checked
        # so the SQL ordering moves on.
        self._upsert(rec)

        if is_terminal:
            self._emit_receipt_audit(txid, cand.get("idempotency_key"), status, block, error)

        log.info(
            "[POLLER] %s | txid=%s status=%s block=%s%s",
            "RESOLVED" if is_terminal else "ticked",
            txid, status, block, f" err={error}" if error else "",
        )

    def _emit_receipt_audit(
        self,
        txid: str,
        idempotency_key: str | None,
        status: str,
        block: int | None,
        error: str | None,
    ) -> None:
        """Write a RECEIPT_RESOLVED audit row.

        Best-effort: if the audit write fails, log it and continue —
        unlike /send, the poller cannot return 5xx to a caller. The
        next tick will see no resolved_at row and try again."""
        try:
            details_parts = [f"status={status}"]
            if block:
                details_parts.append(f"block={block}")
            if error:
                details_parts.append(f"err={error}")
            audit.record(
                "RECEIPT_RESOLVED",
                txid=txid,
                idempotency_key=idempotency_key or "",
                result=status.lower(),
                details=" ".join(details_parts),
            )
        except Exception as exc:
            log.error(
                "[POLLER] audit write for RECEIPT_RESOLVED failed (txid=%s): %s",
                txid, exc,
            )

    # ------ public read API -----------------------------------------------

    def get_status(self, txid: str) -> TxStatus | None:
        """Look up a tx_status row by txid. Used by /api/v1/tx/{txid}/status."""
        if self._conn is not None:
            cur = self._conn.execute(
                """
                SELECT txid, idempotency_key, first_seen, last_checked,
                       status, block_number, last_error, resolved_at
                FROM tx_status WHERE txid = ?
                """,
                (txid,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return TxStatus(*r)
        rec = self._memory.get(txid)
        if not rec:
            return None
        return TxStatus(
            txid=rec["txid"],
            idempotency_key=rec.get("idempotency_key"),
            first_seen=rec.get("first_seen"),
            last_checked=rec.get("last_checked"),
            status=rec.get("status"),
            block_number=rec.get("block_number"),
            last_error=rec.get("last_error"),
            resolved_at=rec.get("resolved_at"),
        )

    def lag_seconds(self) -> float:
        """Seconds since the oldest unresolved committed txid was committed.

        Used by the poll-lag gauge. Returns 0.0 if everything is resolved."""
        if self._conn is None:
            return 0.0
        cur = self._conn.execute(
            """
            SELECT MIN(i.created_at)
            FROM idempotency i
            LEFT JOIN tx_status s ON s.txid = i.txid
            WHERE i.status='committed' AND s.resolved_at IS NULL
            """
        )
        r = cur.fetchone()
        if not r or r[0] is None:
            return 0.0
        return max(0.0, time.time() - r[0])

    # ------ test helpers ---------------------------------------------------

    def _seed_for_tests(self, txid: str, idempotency_key: str | None = None,
                         *, first_seen: float | None = None) -> None:
        """Inject a candidate into the in-memory store. Test-only."""
        self._memory[txid] = {
            "txid": txid,
            "idempotency_key": idempotency_key,
            "first_seen": first_seen if first_seen is not None else time.time(),
            "last_checked": None,
            "status": None,
            "block_number": None,
            "last_error": None,
            "resolved_at": None,
        }

    def _reset_for_tests(self) -> None:
        self._memory.clear()
        if self._conn is not None:
            self._conn.execute("DELETE FROM tx_status")


# ---------------------------------------------------------------------------
# Module-level singleton (initialised at lifespan boot)
# ---------------------------------------------------------------------------


poller: ReceiptPoller | None = None


def init_poller(db_path: str | None, tron_client) -> ReceiptPoller:
    """Construct + start the singleton poller. Called by lifespan."""
    global poller
    if poller is not None:
        try:
            poller.stop()
        except Exception:
            pass
    poller = ReceiptPoller(db_path=db_path, tron_client=tron_client)
    poller.start()
    return poller


def shutdown_poller() -> None:
    global poller
    if poller is None:
        return
    poller.stop()
    poller = None
