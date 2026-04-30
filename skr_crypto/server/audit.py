"""Audit trail for financial operations.

Every record is a single JSON line. Records are written to:
  - the "payouts.audit" logger (always, picked up by stdout) — for ops/SIEM
  - if AUDIT_LOG_FILE is set, also to that file with line-buffered IO and
    fsync per record — for compliance/durability

Record IDs are <process_uuid_prefix>-<seq>: the prefix uniquely identifies
this process run, the seq is monotonically increasing within it. This means
records from multiple runs can be merged without seq collisions and the
ordering inside one run is preserved.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import UTC, datetime

from skr_crypto.server.config import AUDIT_LOG_FILE

audit_log = logging.getLogger("payouts.audit")

_seq_lock = threading.Lock()
_seq_counter = 0
_process_id = uuid.uuid4().hex[:8]


class AuditWriteError(Exception):
    """Durable audit configured but write/fsync failed.

    Raised from `record()` when AUDIT_LOG_FILE is set and we can't
    persist the entry. Treated as a hard error: a financial action
    without a guaranteed audit trail is worse than a failed request.
    The caller should let it propagate so the API returns 5xx and
    the operator notices immediately.
    """

# File handle for durable audit. Opened lazily on first record so import is
# side-effect-free (matters for tests, scripts).
_file_lock = threading.Lock()
_file_fp = None  # type: ignore[var-annotated]
_file_open_failed = False


def _next_record_id() -> str:
    global _seq_counter
    with _seq_lock:
        _seq_counter += 1
        return f"{_process_id}-{_seq_counter}"


def _get_audit_file():
    """Return the durable audit file handle, opening it on first use.

    Returns None if AUDIT_LOG_FILE is not configured or the open failed
    (we already logged the error). We never try to reopen — if the disk is
    gone the operator needs to know now, not get retry storms in the hot path.
    """
    global _file_fp, _file_open_failed
    if not AUDIT_LOG_FILE or _file_open_failed:
        return None
    if _file_fp is not None:
        return _file_fp
    with _file_lock:
        if _file_fp is not None:
            return _file_fp
        try:
            # Append + line-buffered text mode. We fsync explicitly per record
            # so even crashes inside Python won't lose the latest lines.
            _file_fp = open(AUDIT_LOG_FILE, "a", buffering=1, encoding="utf-8")
            audit_log.info(
                json.dumps({
                    "audit_init": True,
                    "process_id": _process_id,
                    "file": AUDIT_LOG_FILE,
                    "timestamp": datetime.now(UTC).isoformat(),
                })
            )
        except Exception as exc:
            _file_open_failed = True
            audit_log.error(
                "Failed to open AUDIT_LOG_FILE=%s: %s — durable audit DISABLED",
                AUDIT_LOG_FILE, exc,
            )
            return None
        return _file_fp


def _write_durable(line: str) -> None:
    fp = _get_audit_file()
    if fp is None:
        # AUDIT_LOG_FILE empty → operator opted out, nothing to do.
        # File open failed earlier → refuse to silently continue: a
        # configured-but-broken audit is worse than no audit. Surface as
        # a hard error so the request fails and the operator notices.
        if _file_open_failed:
            raise AuditWriteError(
                f"Durable audit configured ({AUDIT_LOG_FILE}) but file "
                f"open failed earlier — refusing to write"
            )
        return
    try:
        with _file_lock:
            fp.write(line + "\n")
            fp.flush()
            os.fsync(fp.fileno())
    except Exception as exc:
        # Make noise on stdout/SIEM regardless, then raise so the caller
        # turns it into a 5xx. A money-mover without a durable audit
        # trail must fail loudly, not silently.
        audit_log.error("Audit fsync failed: %s — line=%s", exc, line[:200])
        raise AuditWriteError(f"audit write failed: {exc}") from exc


def record(
    event: str,
    *,
    wallet: str = "",
    from_address: str = "",
    to_address: str = "",
    amount: str = "",
    asset: str = "USDT",
    txid: str = "",
    idempotency_key: str = "",
    client_ip: str = "",
    result: str = "",
    details: str = "",
) -> None:
    """Write a structured audit entry.

    This is separate from operational logs — audit records are the
    immutable trail of every financial action for compliance.

    The ``wallet`` field (added in 1.4.0 for multi-wallet awareness)
    carries the operator-friendly source-wallet name. ``from_address``
    is still recorded — they're complementary, not redundant.
    """
    entry = {
        "id": _next_record_id(),
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        "wallet": wallet,
        "from_address": from_address,
        "to_address": to_address,
        "amount": amount,
        "asset": asset,
        "txid": txid,
        "idempotency_key": idempotency_key,
        "client_ip": client_ip,
        "result": result,
    }
    if details:
        entry["details"] = details

    line = json.dumps(entry, ensure_ascii=False)
    # Always log as JSON regardless of LOG_FORMAT — audit is machine-readable.
    audit_log.info(line)
    # And, if configured, write durably so a crash can't lose the trail.
    _write_durable(line)


def close_audit_file() -> None:
    """Flush + close the durable audit file. Safe to call multiple times."""
    global _file_fp
    with _file_lock:
        if _file_fp is not None:
            try:
                _file_fp.flush()
                os.fsync(_file_fp.fileno())
                _file_fp.close()
            except Exception:
                pass
            _file_fp = None
