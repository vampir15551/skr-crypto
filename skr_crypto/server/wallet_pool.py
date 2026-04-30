"""Pool of loaded wallets — lookup, listing, auto-pick by balance.

The pool is the single source of truth for "which wallets does this
process know about?". It's populated once at startup by the configured
``KeyProvider`` and is read-only after that — adding/removing wallets
requires editing the underlying secret store and restarting the
service. (Operator-time mutations live in the CLI's ``wallet`` group.)

Design choices:

  - **Singleton in production, mockable in tests.** Mirrors how
    ``tron_client.tron`` is structured — tests reset state via a
    helper rather than spinning up a fresh process.
  - **Resolution policy** is centralised here so every endpoint
    behaves the same way. The rules are:

      1. ``name`` provided → must match exactly (else ``WalletNotFound``).
      2. ``name`` is None and only one wallet is loaded → that wallet.
      3. ``name`` is None and ``policy == "max-usdt"`` (default) → the
         wallet with the largest USDT balance, fetched live.
         Wallets that fail the balance RPC are skipped with a warning;
         if all fail we raise ``WalletAutoPickFailed``.

  - **Balance lookup is lazy.** We don't cache USDT balances on the
    pool because they go stale fast. Auto-pick costs N RPC calls per
    /send when N > 1; for a single wallet it's the trivial path.
  - **No locking around the wallets dict** because it's frozen after
    ``init()``. If we add hot-rotation we'll need an RLock here.

The pool stays keyless of an HTTP client: callers pass the live
``tron`` (TronClient) for balance lookups. This avoids a circular
import between wallet_pool ↔ tron_client.
"""
from __future__ import annotations

import logging
import threading
from decimal import Decimal

from skr_crypto.server.wallet import Wallet

log = logging.getLogger("payouts")


# ---------------------------------------------------------------------------
# Errors — caller-friendly distinct types so endpoints can return precise
# 4xx codes instead of generic 500s.
# ---------------------------------------------------------------------------


class WalletPoolError(Exception):
    """Base class."""


class WalletNotFound(WalletPoolError):
    """Caller asked for a name that isn't in the pool."""

    def __init__(self, name: str, available: list[str]) -> None:
        super().__init__(
            f"wallet {name!r} not in pool (available: {', '.join(available) or 'none'})"
        )
        self.name = name
        self.available = list(available)


class WalletPoolEmpty(WalletPoolError):
    """No wallets loaded — service can't sign anything."""


class WalletAutoPickFailed(WalletPoolError):
    """Auto-pick wanted to consult balances but every RPC failed."""


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


class WalletPool:
    """Process-wide registry of loaded wallets."""

    def __init__(self) -> None:
        self._wallets: dict[str, Wallet] = {}
        # init() is split out from __init__ so tests can construct an
        # empty pool and inject wallets directly.
        self._initialized: bool = False
        # Single lock for init/destroy so a race between lifespan
        # startup and a request can't see a half-built pool.
        self._lock = threading.Lock()

    # ----- lifecycle ---------------------------------------------------------

    def init(self, wallets: list[Wallet]) -> None:
        """Populate from a pre-loaded list. Called by ``security.load_wallets``.

        Idempotent: a second call replaces the contents (used by tests
        and by hot-restart paths).
        """
        with self._lock:
            self._wallets = {w.name: w for w in wallets}
            self._initialized = True
            if not self._wallets:
                log.error("WalletPool initialised with zero wallets")
                return
            log.info(
                "WalletPool initialised with %d wallet(s): %s",
                len(self._wallets),
                ", ".join(f"{w.name}={w.address}" for w in self._wallets.values()),
            )

    def destroy(self) -> None:
        """Drop all private keys. Called from the lifespan shutdown path."""
        with self._lock:
            for w in self._wallets.values():
                w.destroy()
            self._wallets.clear()
            self._initialized = False

    # ----- read API ----------------------------------------------------------

    def is_initialized(self) -> bool:
        return self._initialized

    def names(self) -> list[str]:
        """Sorted list of wallet names. Stable for UI / API responses."""
        return sorted(self._wallets.keys())

    def all(self) -> list[Wallet]:
        """All wallets in name-sorted order."""
        return [self._wallets[n] for n in self.names()]

    def count(self) -> int:
        return len(self._wallets)

    def get(self, name: str) -> Wallet:
        """Lookup by exact name. Raises ``WalletNotFound`` if missing."""
        w = self._wallets.get(name)
        if w is None:
            raise WalletNotFound(name, self.names())
        return w

    # ----- resolution: "what wallet does this request use?" -----------------

    def resolve(
        self,
        name: str | None,
        *,
        tron_client=None,
        policy: str = "max-usdt",
    ) -> Wallet:
        """Pick the wallet for an inbound request.

        ``name``     explicit name from the request (may be None).
        ``tron_client`` the keyless RPC client, used only for the
                     auto-pick balance lookups. Pass None to forbid
                     auto-pick (callers that don't want network).
        ``policy``   currently only ``"max-usdt"`` is supported.

        Raises:
            WalletPoolEmpty            — pool has no wallets at all
            WalletNotFound             — explicit name didn't match
            WalletAutoPickFailed       — every balance lookup failed
            WalletPoolError            — auto-pick requested without a
                                         tron_client when N > 1
        """
        if not self._wallets:
            raise WalletPoolEmpty(
                "no wallets loaded — cannot resolve send request"
            )

        if name:
            return self.get(name)

        # name is None — single-wallet shortcut.
        if len(self._wallets) == 1:
            return next(iter(self._wallets.values()))

        # Multi-wallet auto-pick.
        if policy != "max-usdt":
            raise WalletPoolError(f"unknown auto-pick policy: {policy!r}")
        if tron_client is None:
            raise WalletPoolError(
                "multi-wallet pool needs an explicit `wallet` parameter "
                "or a tron_client for auto-pick"
            )

        return self._auto_pick_max_usdt(tron_client)

    def _auto_pick_max_usdt(self, tron_client) -> Wallet:
        """Pick the wallet with the largest USDT balance.

        On RPC failure for a particular wallet we log and skip — a
        single transient 5xx must not nuke the whole /send. If every
        wallet's lookup fails we raise so the caller returns 503-ish.
        """
        best: Wallet | None = None
        best_balance: Decimal = Decimal(-1)
        any_ok = False
        for w in self.all():
            try:
                bal = tron_client.get_usdt_balance_for(w.address)
            except Exception as exc:
                log.warning(
                    "auto-pick: USDT balance lookup failed for wallet=%s "
                    "address=%s (%s): %s — skipping",
                    w.name, w.address, type(exc).__name__, exc,
                )
                continue
            any_ok = True
            log.info(
                "auto-pick: wallet=%s address=%s usdt=%s",
                w.name, w.address, bal,
            )
            if bal > best_balance:
                best = w
                best_balance = bal

        if not any_ok or best is None:
            raise WalletAutoPickFailed(
                "could not fetch USDT balance for any wallet"
            )

        log.info(
            "auto-pick winner: wallet=%s address=%s usdt=%s",
            best.name, best.address, best_balance,
        )
        return best

    # ----- test helpers (do not call from production code) -----------------

    def _reset_for_tests(self) -> None:
        """Wipe the pool. Used by ``conftest`` to start each test fresh."""
        with self._lock:
            self._wallets.clear()
            self._initialized = False

    def _override_for_tests(self, wallets: list[Wallet]) -> None:
        """Replace pool contents directly; for tests that want a known set."""
        with self._lock:
            self._wallets = {w.name: w for w in wallets}
            self._initialized = True


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

wallets = WalletPool()
