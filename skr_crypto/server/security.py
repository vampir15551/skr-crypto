"""Memory-safe key handling and X-API-Key auth.

This module is the bridge between :mod:`skr_crypto.server.key_providers`
(which knows how to fetch raw key bytes from various secret stores) and
:mod:`skr_crypto.server.wallet` / :mod:`skr_crypto.server.wallet_pool`
(which present them to the rest of the service).

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
"""
from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request
from tronpy.keys import PrivateKey

from skr_crypto.server.config import AUTH_TOKEN
from skr_crypto.server.key_providers import KeyProvider, get_key_provider
from skr_crypto.server.wallet import Wallet

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
# this attribute (or, more typically, mock the WalletPool directly).
_provider: KeyProvider = get_key_provider()


def load_wallets() -> list[Wallet]:
    """Load every configured wallet via the active provider.

    Each ``LoadedWallet`` (raw bytes) becomes a ``Wallet`` (PrivateKey +
    derived address). The raw bytearray is wiped immediately after the
    PrivateKey copy is constructed.

    If the provider's advertised address disagrees with the derived one,
    we log a warning and trust the derived address — providers cannot
    sign, only the derived path can.
    """
    loaded = _provider.load_wallets()
    out: list[Wallet] = []
    seen: set[str] = set()
    for entry in loaded:
        if entry.name in seen:
            log.error("Duplicate wallet name from provider: %r", entry.name)
            wipe_bytearray(entry.raw_key)
            raise RuntimeError(f"duplicate wallet name: {entry.name}")
        seen.add(entry.name)

        priv = PrivateKey(bytes(entry.raw_key))
        derived_address = priv.public_key.to_base58check_address()
        if entry.address and entry.address != derived_address:
            log.warning(
                "Wallet %r: provider advertised address %s but derived %s — "
                "using derived (provider hint may be stale)",
                entry.name, entry.address, derived_address,
            )

        wipe_bytearray(entry.raw_key)
        out.append(Wallet(
            name=entry.name,
            address=derived_address,
            priv_key=priv,
        ))

    return out


def lock_key_store() -> None:
    """Best-effort lock of the underlying secret backend.

    For the 1Password backend this signs out all sessions; for env / file
    / keychain / encrypted_file backends it is a no-op. Called at process
    exit by the lifespan handler.
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
