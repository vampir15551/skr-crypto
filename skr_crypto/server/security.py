"""Memory-safe key handling and X-API-Key auth.

This module is now a thin wrapper around :mod:`app.key_providers`.
Backends (1Password / env / file / keychain) are selected via the
``KEY_PROVIDER`` config; see ``key_providers.py`` for the actual flows.

Honest disclosure on what wipe really buys you in CPython:

  - We read the key as raw bytes (no Python ``str`` decode) so the worst
    case where CPython interns or codec-buffers the hex value can't
    happen. Backends honour this as best the underlying interface allows.

  - We hex-decode bytes → bytearray, the only Python type whose backing
    buffer we can deterministically zero. ``wipe_bytearray`` does that.

  - We then construct ``tronpy.PrivateKey`` from ``bytes(bytearray)``.
    That makes a copy that lives inside the PrivateKey object until
    ``destroy()``. CPython's GC will eventually reclaim it but does NOT
    zero the memory. There is no portable way to wipe immutable bytes
    in CPython — this is a CPython limitation, not a code smell.

  - The strongest mitigation we offer is: short-lived process (default
    ~10 min via auto-shutdown), the provider's ``lock()`` on exit,
    and never swapping (run on a memory-only filesystem if needed).

In short: ``wipe_bytearray`` is real; the rest is best-effort and bounded
by the lifetime of the process.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request

from skr_crypto.server.config import AUTH_TOKEN
from skr_crypto.server.key_providers import KeyProvider, get_key_provider

log = logging.getLogger("payouts")


def wipe_bytearray(buf: bytearray) -> None:
    """Overwrite a bytearray with zeros."""
    for i in range(len(buf)):
        buf[i] = 0


# ---------------------------------------------------------------------------
# Module-level provider singleton
# ---------------------------------------------------------------------------
# Resolved once at import time so the choice of backend is logged exactly
# once at startup. Tests that need a different provider can monkey-patch
# this attribute (or, more typically, mock TronClient.init() directly).
_provider: KeyProvider = get_key_provider()


def load_private_key() -> bytearray:
    """Load the TRON private key via the configured backend."""
    return _provider.get_private_key()


def lock_key_store() -> None:
    """Best-effort lock of the underlying secret backend.

    For the 1Password backend this signs out all sessions; for env/file/
    keychain backends it is a no-op. Called at process exit by the
    lifespan handler.
    """
    _provider.lock()


# ---------------------------------------------------------------------------
# X-API-Key auth dependency
# ---------------------------------------------------------------------------


def verify_api_key(request: Request) -> str:
    """Validate X-API-Key header (constant-time comparison)."""
    if not AUTH_TOKEN:
        raise HTTPException(500, "AUTH_TOKEN not configured")

    api_key = request.headers.get("X-API-Key", "")
    if not api_key or not hmac.compare_digest(api_key, AUTH_TOKEN):
        raise HTTPException(401, "Invalid or missing X-API-Key")

    return api_key
