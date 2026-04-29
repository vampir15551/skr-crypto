"""Idempotency stores.

Two implementations with identical semantics:

  - IdempotencyStore       — in-memory dict + Condition. Fast. State lost
                              on process restart.
  - SqliteIdempotencyStore — durable file-backed. State survives restart.
                              Orphaned PENDING rows (reserved but never
                              committed because the process crashed) are
                              promoted to UNKNOWN at startup, so a retry
                              can't silently re-broadcast.

Lifecycle of a key (shared by both implementations):

    reserve(key) -> None        : key is ours; must call commit() or release()
    reserve(key) -> "<txid>"    : key already committed; return as duplicate
    reserve(key) -> blocks ...  : another request in the same process is
                                  holding it; waits up to
                                  _RESERVE_WAIT_TIMEOUT then re-checks
    reserve(key) -> raises      : IdempotencyConflict (timeout) or
                                  UnresolvedIdempotency (recovered orphan)
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid

from skr_crypto.server.config import IDEMPOTENCY_DB_PATH

log = logging.getLogger("payouts")

# Sentinel stored in place of a txid while a request is in flight.
# Not a valid TRON txid (those are 64 hex chars), so it can never collide.
_PENDING = "__PENDING__"

# How long a concurrent request waits for an in-flight reservation to
# commit/release before giving up.
_RESERVE_WAIT_TIMEOUT = 60.0  # seconds

# Row status values for the SQLite store.
_STATUS_PENDING = "pending"
_STATUS_COMMITTED = "committed"
_STATUS_UNKNOWN = "unknown"


class IdempotencyConflict(Exception):
    """Timed out waiting for an in-flight reservation to complete."""


class UnresolvedIdempotency(Exception):
    """Raised when an idempotency key is in 'unknown' state — a previous
    process crashed after reserving but before committing, and we don't
    know whether the broadcast actually hit the chain.

    Client MUST check on-chain state (tronscan) before retrying with the
    same key. Retrying blindly risks a double-send.
    """

    def __init__(self, key: str, created_at: float):
        self.key = key
        self.created_at = created_at
        super().__init__(
            f"Idempotency key {key!r} is in UNKNOWN state (prior process "
            f"crashed mid-broadcast at {created_at}). Check tronscan for "
            f"a transaction with this from_address before retrying — "
            f"broadcasting the same key again risks a double-send."
        )


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


class IdempotencyStore:
    """In-memory implementation. See module docstring for semantics."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._store: dict[str, str] = {}

    def reserve(self, key: str) -> str | None:
        with self._cond:
            while True:
                existing = self._store.get(key)
                if existing is None:
                    self._store[key] = _PENDING
                    return None
                if existing != _PENDING:
                    return existing
                got = self._cond.wait(timeout=_RESERVE_WAIT_TIMEOUT)
                if not got:
                    raise IdempotencyConflict(
                        f"Timed out waiting for in-flight request to complete for key {key!r}"
                    )

    def commit(self, key: str, txid: str) -> None:
        if not txid or txid == _PENDING:
            raise ValueError("commit requires a non-empty real txid")
        with self._cond:
            self._store[key] = txid
            self._cond.notify_all()
            log.info("Idempotency key committed: %s -> %s", key, txid)

    def release(self, key: str) -> None:
        with self._cond:
            if self._store.get(key) == _PENDING:
                del self._store[key]
                self._cond.notify_all()

    def peek(self, key: str) -> str | None:
        with self._cond:
            v = self._store.get(key)
            if v is None or v == _PENDING:
                return None
            return v

    def count(self) -> int:
        with self._cond:
            return len(self._store)

    def close(self) -> None:
        """No-op for in-memory store."""
        pass

    def _reset_for_tests(self) -> None:
        with self._cond:
            self._store.clear()
            self._cond.notify_all()


# ---------------------------------------------------------------------------
# SQLite-backed durable store
# ---------------------------------------------------------------------------


class SqliteIdempotencyStore:
    """Durable file-backed implementation.

    Crash model:
      - commit is a single UPDATE with synchronous=FULL + WAL — either the
        row is 'committed' with the txid, or it isn't.
      - If the process dies after reserve but before commit/release, the row
        stays 'pending'. Next startup promotes all 'pending' rows to
        'unknown'. A retry with that key then raises UnresolvedIdempotency
        instead of silently reserving anew (which could double-send).

    Concurrency model:
      - One shared connection (sqlite3 with check_same_thread=False).
      - All writes are serialized by Python's Condition, so we never rely on
        SQLite's own locking for correctness — we use it only for crash
        atomicity.
      - Same-process same-key waiters block on the Condition (fast, no
        SQLite contention).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        # Unique per process run — used in diagnostics, logged in audit init.
        self._process_id = uuid.uuid4().hex[:8]
        self._cond = threading.Condition(threading.Lock())
        # check_same_thread=False so multiple uvicorn worker threads can use
        # the same connection; the Condition serializes access.
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            db_path, check_same_thread=False, timeout=30.0,
        )
        # WAL + synchronous=FULL: every commit() issues an fsync on the WAL,
        # so a crash/power-loss cannot lose an acknowledged write.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._init_schema()
        self._recover_orphans()

    def _init_schema(self) -> None:
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS idempotency (
                key         TEXT PRIMARY KEY,
                txid        TEXT NOT NULL,
                status      TEXT NOT NULL,
                process_id  TEXT NOT NULL,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_idem_status ON idempotency(status);
            """
        )
        self._conn.commit()

    def _recover_orphans(self) -> int:
        """Promote any leftover 'pending' rows to 'unknown'.

        Called once at startup. Rationale: a 'pending' row means some prior
        run reserved the key but never committed or released — usually
        because the process died between the broadcast call and the
        subsequent commit. We CAN'T tell whether the tx actually made it
        to the chain. The safe thing is to refuse silent retries: future
        reserve() calls on those keys will raise UnresolvedIdempotency,
        making the operator reconcile.
        """
        assert self._conn is not None
        now = time.time()
        with self._cond:
            cur = self._conn.execute(
                "UPDATE idempotency "
                "SET status=?, updated_at=? "
                "WHERE status=?",
                (_STATUS_UNKNOWN, now, _STATUS_PENDING),
            )
            self._conn.commit()
            n = cur.rowcount
        if n > 0:
            log.warning(
                "Recovered %d orphaned PENDING idempotency keys as UNKNOWN "
                "(previous process crashed mid-broadcast). Operator must "
                "reconcile via tronscan before retrying with those keys.",
                n,
            )
        return n

    def _fetch_row(self, key: str):
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT status, txid, created_at FROM idempotency WHERE key=?",
            (key,),
        )
        return cur.fetchone()

    def reserve(self, key: str) -> str | None:
        with self._cond:
            assert self._conn is not None
            while True:
                row = self._fetch_row(key)
                if row is None:
                    # Free — claim with an INSERT. If two same-process
                    # threads race past fetch_row, the UNIQUE constraint
                    # catches the second, and we loop.
                    now = time.time()
                    try:
                        self._conn.execute(
                            "INSERT INTO idempotency "
                            "(key, txid, status, process_id, created_at, updated_at) "
                            "VALUES (?, '', ?, ?, ?, ?)",
                            (key, _STATUS_PENDING, self._process_id, now, now),
                        )
                        self._conn.commit()
                        return None
                    except sqlite3.IntegrityError:
                        continue  # Someone else beat us — re-read.

                status, txid, created_at = row
                if status == _STATUS_COMMITTED:
                    return txid
                if status == _STATUS_UNKNOWN:
                    raise UnresolvedIdempotency(key, created_at)
                # status == pending: in-flight in our process. Wait.
                got = self._cond.wait(timeout=_RESERVE_WAIT_TIMEOUT)
                if not got:
                    raise IdempotencyConflict(
                        f"Timed out waiting for in-flight request to complete for key {key!r}"
                    )

    def commit(self, key: str, txid: str) -> None:
        if not txid or txid == _PENDING:
            raise ValueError("commit requires a non-empty real txid")
        with self._cond:
            assert self._conn is not None
            now = time.time()
            self._conn.execute(
                "UPDATE idempotency "
                "SET txid=?, status=?, updated_at=? "
                "WHERE key=?",
                (txid, _STATUS_COMMITTED, now, key),
            )
            self._conn.commit()
            self._cond.notify_all()
            log.info("Idempotency key committed (sqlite): %s -> %s", key, txid)

    def release(self, key: str) -> None:
        with self._cond:
            assert self._conn is not None
            cur = self._conn.execute(
                "DELETE FROM idempotency WHERE key=? AND status=?",
                (key, _STATUS_PENDING),
            )
            self._conn.commit()
            if cur.rowcount > 0:
                self._cond.notify_all()

    def peek(self, key: str) -> str | None:
        with self._cond:
            row = self._fetch_row(key)
            if row is None:
                return None
            status, txid, _ = row
            if status == _STATUS_COMMITTED:
                return txid
            return None

    def count(self) -> int:
        with self._cond:
            assert self._conn is not None
            cur = self._conn.execute("SELECT COUNT(*) FROM idempotency")
            return cur.fetchone()[0]

    def close(self) -> None:
        """Flush and close the underlying SQLite connection.

        Safe to call multiple times. After close, every method will raise
        AssertionError — the object should be discarded.
        """
        with self._cond:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except Exception as exc:
                    log.warning("SqliteIdempotencyStore close failed: %s", exc)
                self._conn = None
                self._cond.notify_all()

    def _reset_for_tests(self) -> None:
        """Truncate everything. Only safe in tests."""
        with self._cond:
            assert self._conn is not None
            self._conn.execute("DELETE FROM idempotency")
            self._conn.commit()
            self._cond.notify_all()


# ---------------------------------------------------------------------------
# Factory / singleton
# ---------------------------------------------------------------------------


def _build_store():
    """Build the idempotency store based on config.

    Called once at module import. If IDEMPOTENCY_DB_PATH is set and the
    SQLite store fails to open, we fall back to in-memory with a loud
    warning — better to keep serving than to deadlock the entire service.

    The in-memory default is also flagged at WARNING. Silent fallback
    is dangerous for a money-mover: the operator could spend weeks
    thinking idempotency survives restart when it doesn't, and any
    retry across a restart can double-broadcast.
    """
    if not IDEMPOTENCY_DB_PATH:
        log.warning(
            "Idempotency store: IN-MEMORY (IDEMPOTENCY_DB_PATH unset) — "
            "state will NOT survive restart, retries across restart can "
            "double-broadcast. Set IDEMPOTENCY_DB_PATH for production."
        )
        return IdempotencyStore()
    try:
        store = SqliteIdempotencyStore(IDEMPOTENCY_DB_PATH)
        log.info("Idempotency store: SQLite at %s", IDEMPOTENCY_DB_PATH)
        return store
    except Exception as exc:
        log.error(
            "Failed to open SQLite idempotency store at %s: %s — "
            "falling back to IN-MEMORY (state will NOT survive restart!)",
            IDEMPOTENCY_DB_PATH, exc,
        )
        return IdempotencyStore()


idempotency = _build_store()
