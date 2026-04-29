from __future__ import annotations

import logging
import os
import signal
import threading
import time

from skr_crypto.server.config import SHUTDOWN_TIMEOUT
from skr_crypto.server.security import lock_key_store

log = logging.getLogger("payouts")

_lock = threading.Lock()
_timer: threading.Timer | None = None
_last_activity: float = 0.0
_start_time: float = 0.0


def _on_timeout() -> None:
    log.warning("Inactivity timeout (%ds) — shutting down", SHUTDOWN_TIMEOUT)
    # Import here to avoid circular import at module level
    from skr_crypto.server.audit import close_audit_file
    from skr_crypto.server.idempotency import idempotency
    from skr_crypto.server.tron_client import tron
    tron.destroy()
    # Close SQLite store (if enabled) so on-disk state is consistent even if
    # the signal path below gets interrupted. in-memory close() is a no-op.
    try:
        idempotency.close()
    except Exception as exc:
        log.warning("Idempotency close failed: %s", exc)
    close_audit_file()
    lock_key_store()
    os.kill(os.getpid(), signal.SIGTERM)


def mark_started() -> None:
    global _last_activity, _start_time
    now = time.time()
    _last_activity = now
    _start_time = now


def reset_timer() -> None:
    global _timer, _last_activity
    with _lock:
        _last_activity = time.time()
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(SHUTDOWN_TIMEOUT, _on_timeout)
        _timer.daemon = True
        _timer.start()


def uptime() -> int:
    return int(time.time() - _start_time)


def seconds_remaining() -> int:
    return max(0, int(SHUTDOWN_TIMEOUT - (time.time() - _last_activity)))
