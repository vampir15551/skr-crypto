"""OFAC SDN sanctions list — local TRON address blocklist.

The OFAC Specially Designated Nationals list includes crypto addresses
tied to sanctioned entities (Tornado Cash, Lazarus Group, Iranian
state actors, etc.). It's the **gold standard for "is this address
under US sanctions"** and unlike commercial AML services it has
**zero rate limits** because we read a static list, not an API.

Source: ``0xB10C/ofac-sanctioned-digital-currency-addresses`` on
GitHub — a community-maintained mirror of the official Treasury list,
updated whenever OFAC publishes a new SDN. We download the TRX-tagged
file once at process startup and cache the parsed set in memory for
the process lifetime.

Failure modes:

  - URL unreachable at boot → keep using the on-disk cache (if any),
    otherwise the list is empty and the ``sanctions`` check returns
    SKIP (visible in the report — operator can see we couldn't load).
  - First-ever boot with no network → empty list, SKIP. The service
    still comes up; the OFAC gate just doesn't fire until the next
    restart.

Refresh cadence: per-process. ``SHUTDOWN_TIMEOUT`` of 600s in dev
means a fresh list every 10 minutes; under systemd it's whatever the
restart cadence is. Set ``SANCTIONS_LIST_REFRESH=false`` to use
the bundled cache and never hit the network.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import requests

from skr_crypto.server.config import (
    SANCTIONS_LIST_REFRESH,
    SANCTIONS_LIST_URL,
    SKR_CRYPTO_HOME,
)

log = logging.getLogger("payouts")


_lock = threading.Lock()
_addresses: frozenset[str] | None = None
_loaded_from: str = "not loaded"  # "url" / "cache" / "empty"


# Where to persist the last successful download. Lives in
# $SKR_CRYPTO_HOME/data/ so it survives restarts but is .gitignored
# (see project root .gitignore). Treated as a cache, not a source of
# truth — the URL refresh is what's authoritative.
def _cache_path() -> Path:
    base = Path(SKR_CRYPTO_HOME) if SKR_CRYPTO_HOME else Path.home() / ".skr-crypto"
    return base / "data" / "ofac-sdn-trx.txt"


def load(*, timeout: float = 10.0) -> int:
    """Load (or refresh) the sanctions list. Idempotent.

    Returns the number of addresses loaded. Safe to call multiple
    times — the second call updates the cache.
    """
    global _addresses, _loaded_from

    with _lock:
        addresses: set[str] = set()
        cache = _cache_path()

        # 1. Try the URL (unless explicitly disabled).
        if SANCTIONS_LIST_REFRESH:
            try:
                response = requests.get(
                    SANCTIONS_LIST_URL,
                    timeout=timeout,
                    headers={"User-Agent": "skr-crypto-sanctions/1"},
                )
                response.raise_for_status()
                addresses = _parse(response.text)
                # Persist to disk so the next boot has a fallback.
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(response.text, encoding="utf-8")
                except OSError as exc:
                    log.warning(
                        "[SANCTIONS] could not write cache to %s: %s", cache, exc,
                    )
                _addresses = frozenset(addresses)
                _loaded_from = "url"
                log.info(
                    "[SANCTIONS] loaded %d TRX addresses from %s",
                    len(addresses), SANCTIONS_LIST_URL,
                )
                return len(addresses)
            except Exception as exc:
                log.warning(
                    "[SANCTIONS] could not fetch %s (%s) — falling back to cache",
                    SANCTIONS_LIST_URL, type(exc).__name__,
                )

        # 2. Fall back to disk cache.
        if cache.exists():
            try:
                addresses = _parse(cache.read_text(encoding="utf-8"))
                _addresses = frozenset(addresses)
                _loaded_from = "cache"
                log.warning(
                    "[SANCTIONS] using stale on-disk cache at %s "
                    "(%d addresses) — set SANCTIONS_LIST_REFRESH=true and "
                    "ensure network access for fresh data",
                    cache, len(addresses),
                )
                return len(addresses)
            except Exception as exc:
                log.error(
                    "[SANCTIONS] cache at %s is corrupt (%s) — empty list",
                    cache, exc,
                )

        # 3. Empty list — sanctions check will SKIP, never fail-open.
        _addresses = frozenset()
        _loaded_from = "empty"
        log.error(
            "[SANCTIONS] no source available — list is empty. "
            "OFAC sanctions check will SKIP until the next refresh."
        )
        return 0


def contains(address: str) -> bool | None:
    """Is ``address`` on the sanctions list?

    Returns:
      - True  — confirmed sanctioned
      - False — confirmed not sanctioned
      - None  — list not loaded (caller should treat as SKIP, not pass)
    """
    if _addresses is None:
        return None
    return address in _addresses


def status() -> dict[str, object]:
    """For doctor / debug introspection."""
    return {
        "loaded": _addresses is not None,
        "loaded_from": _loaded_from,
        "size": len(_addresses) if _addresses is not None else 0,
    }


def _parse(text: str) -> set[str]:
    """Parse one-address-per-line text format.

    Skips blank lines and comments (``#``). Each line is trimmed and
    expected to be a valid base58 TRON address; we don't validate
    here because the assess_risk caller has already validated the
    *query* address — what matters is exact-match against the list.
    """
    out: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    return out


# ---------------------------------------------------------------------------
# Test hooks
# ---------------------------------------------------------------------------


def _override_for_tests(addresses: set[str] | None) -> None:
    """Force the loaded set to ``addresses`` (or unloaded if None).
    Tests use this instead of touching the network."""
    global _addresses, _loaded_from
    if addresses is None:
        _addresses = None
        _loaded_from = "not loaded"
    else:
        _addresses = frozenset(addresses)
        _loaded_from = "test-override"
