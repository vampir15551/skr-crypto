"""Keyless TRON RPC layer.

Holds the TronGrid HTTP client + cached chain artefacts (USDT contract
handle, energy price). Does NOT hold any private key — signing lives
in :class:`skr_crypto.server.wallet.Wallet`. This split lets the
service host multiple wallets behind a single connection pool.

Migrating from the old single-wallet shape:

  - Old: ``tron.address``, ``tron.priv_key``, ``tron.get_trx_balance()``,
    ``tron.send_usdt(to, amount, fee_limit)``.
  - New: address-parameterised reads
    (``get_trx_balance_for(addr)`` / ``get_usdt_balance_for(addr)`` /
    ``get_account_resource_for(addr)``) + signing on the wallet
    (``wallet.send_usdt(client, contract, to, amount, fee_limit)``).

Backward-compat shims are intentionally absent — the multi-wallet
refactor is a hard cut, version bumped accordingly.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from decimal import Decimal

from tronpy import Tron
from tronpy.abi import trx_abi
from tronpy.providers import HTTPProvider

from skr_crypto.server.config import (
    FEE_LIMIT_SAFETY_MULT,
    TRON_ENERGY_PRICE_SUN_FALLBACK,
    TRON_HTTP_TIMEOUT,
    TRON_NETWORK,
    TRONGRID_API_KEY,
    USDT_CONTRACT,
    USDT_DECIMALS,
    USDT_FEE_LIMIT_SUN,
)

# Min fee_limit floor (in SUN). Even if the estimate says ~6 TRX, we never go
# below this — node sometimes spikes energy on contract state changes.
_MIN_FEE_LIMIT_SUN: int = 5_000_000  # 5 TRX

# How long to trust a cached chain energy price before re-fetching.
# EnergyFee changes only via on-chain proposal (months apart historically),
# so even a generous TTL is fine. One hour bounds staleness without
# spamming the node.
_ENERGY_PRICE_TTL_SEC: float = 3600.0

log = logging.getLogger("payouts")

_ENDPOINTS = {
    "mainnet": "https://api.trongrid.io",
    "shasta": "https://api.shasta.trongrid.io",
    "nile": "https://nile.trongrid.io",
}

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


def _is_transient_error(exc: BaseException) -> bool:
    """Detect transient HTTP / network errors that warrant a retry."""
    msg = str(exc)
    if "429" in msg or "Too Many Requests" in msg:
        return True
    if any(c in msg for c in ("500", "502", "503", "504")):
        return True
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name:
        return True
    if "timed out" in msg.lower():
        return True
    return False


def _with_retry(fn, description: str):
    """Retry a callable up to MAX_RETRIES times on transient HTTP errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            if _is_transient_error(exc) and attempt < MAX_RETRIES:
                wait = RETRY_DELAY * attempt
                log.warning("%s failed (attempt %d/%d): %s — retrying in %ds",
                            description, attempt, MAX_RETRIES, type(exc).__name__, wait)
                time.sleep(wait)
                continue
            raise


class TronClient:
    """Keyless TRON RPC client. One per process; safe to call from
    multiple threads (tronpy itself is sync + uses requests under the hood,
    but our caches are threadsafe via the locks below)."""

    def __init__(self) -> None:
        self.client: Tron | None = None
        self._usdt_contract = None
        self._contract_lock = threading.Lock()
        # Cached chain energy price (sun per energy unit). Refreshed lazily
        # on first use and then every _ENERGY_PRICE_TTL_SEC. On RPC failure
        # we keep serving the last known value (better than the configured
        # fallback if we previously had a real reading).
        self._energy_price_sun: int | None = None
        self._energy_price_fetched_at: float = 0.0
        self._energy_price_lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------

    def init(self) -> None:
        endpoint = _ENDPOINTS.get(TRON_NETWORK)
        if not endpoint:
            log.error("Unknown TRON_NETWORK: %s", TRON_NETWORK)
            sys.exit(1)

        # Use API key if provided (avoids 429 rate limits). Always pass an
        # explicit timeout so a stuck node can't hang request handlers.
        if TRONGRID_API_KEY:
            provider = HTTPProvider(endpoint, api_key=TRONGRID_API_KEY, timeout=TRON_HTTP_TIMEOUT)
            log.info("Using TronGrid API key (http timeout=%ss)", TRON_HTTP_TIMEOUT)
        else:
            provider = HTTPProvider(endpoint, timeout=TRON_HTTP_TIMEOUT)
            log.warning("No TRONGRID_API_KEY set — rate limits may apply (http timeout=%ss)",
                        TRON_HTTP_TIMEOUT)

        self.client = Tron(provider=provider)
        log.info("TronClient initialised (network: %s)", TRON_NETWORK)

    def destroy(self) -> None:
        self.client = None
        self._usdt_contract = None

    # -- contract cache ------------------------------------------------------

    def get_usdt_contract(self):
        """Cache the USDT contract object to avoid repeated getcontract calls.

        Public so ``Wallet.send_usdt`` can grab the same handle. Thread-safe:
        double-checked locking ensures only one RPC call even if multiple
        requests race on first init.
        """
        if self._usdt_contract is not None:
            return self._usdt_contract
        with self._contract_lock:
            if self._usdt_contract is None:
                self._usdt_contract = _with_retry(
                    lambda: self.client.get_contract(USDT_CONTRACT),
                    "get_contract(USDT)",
                )
            return self._usdt_contract

    # -- queries (address-parameterised) -------------------------------------

    def get_trx_balance_for(self, address: str) -> Decimal:
        """TRX balance of an arbitrary address. Used by /balance and the
        auto-pick path in WalletPool.

        Returns Decimal("0") if the account has never been activated —
        TronGrid returns "AccountNotFound" for those, which is not an
        error condition for our purposes (the address simply has no
        balance yet)."""
        try:
            return _with_retry(
                lambda: self.client.get_account_balance(address),
                "get_trx_balance",
            )
        except Exception as exc:
            cls = type(exc).__name__.lower()
            if "not found" in str(exc).lower() or "addressnotfound" in cls:
                return Decimal(0)
            raise

    def get_usdt_balance_for(self, address: str) -> Decimal:
        contract = self.get_usdt_contract()
        raw = _with_retry(
            lambda: contract.functions.balanceOf(address),
            "get_usdt_balance",
        )
        return Decimal(raw) / Decimal(10 ** USDT_DECIMALS)

    # -- health ---------------------------------------------------------------

    def check_connection(self) -> bool:
        """Check if TRON node is reachable."""
        try:
            self.client.get_latest_block_number()
            return True
        except Exception:
            return False

    # -- account resources (energy / bandwidth) -----------------------------

    def get_account_resource_for(self, address: str) -> dict:
        """Return raw account resource dict for an address.

        Important fields (mainnet):
          EnergyLimit / EnergyUsed         — staked-derived energy quota
          NetLimit / NetUsed               — staked-derived bandwidth quota
          freeNetLimit / freeNetUsed       — free 600/day bandwidth pool
          tronPowerLimit                   — total staked TRX (in TRX, not sun)
        """
        return _with_retry(
            lambda: self.client.get_account_resource(address),
            "get_account_resource",
        )

    def get_resource_summary_for(self, address: str) -> dict:
        """Operator-friendly summary of energy + bandwidth + stakes."""
        try:
            r = self.get_account_resource_for(address)
        except Exception as exc:
            log.warning("get_account_resource(%s) failed: %s", address, exc)
            return {
                "energy_available": 0,
                "energy_limit": 0,
                "bandwidth_free_available": 0,
                "bandwidth_paid_available": 0,
                "tron_power": 0,
            }

        energy_limit = int(r.get("EnergyLimit", 0) or 0)
        energy_used = int(r.get("EnergyUsed", 0) or 0)
        net_limit = int(r.get("NetLimit", 0) or 0)
        net_used = int(r.get("NetUsed", 0) or 0)
        free_net_limit = int(r.get("freeNetLimit", 0) or 0)
        free_net_used = int(r.get("freeNetUsed", 0) or 0)
        tron_power = int(r.get("tronPowerLimit", 0) or 0)

        return {
            "energy_available": max(0, energy_limit - energy_used),
            "energy_limit": energy_limit,
            "bandwidth_free_available": max(0, free_net_limit - free_net_used),
            "bandwidth_paid_available": max(0, net_limit - net_used),
            "tron_power": tron_power,
        }

    # -- energy price (chain parameter) -------------------------------------

    def energy_price_sun(self) -> int:
        """Return the current chain energy price in SUN per energy unit.

        Cached with TTL=_ENERGY_PRICE_TTL_SEC. After expiry the next call
        re-fetches; on RPC failure we keep serving the last known value
        (better than the configured fallback once we've seen a real one).
        """
        now = time.time()
        cached = self._energy_price_sun
        if cached is not None and (now - self._energy_price_fetched_at) < _ENERGY_PRICE_TTL_SEC:
            return cached
        with self._energy_price_lock:
            now = time.time()
            cached = self._energy_price_sun
            if cached is not None and (now - self._energy_price_fetched_at) < _ENERGY_PRICE_TTL_SEC:
                return cached
            try:
                params = _with_retry(
                    self.client.get_chain_parameters,
                    "get_chain_parameters",
                )
                price = TRON_ENERGY_PRICE_SUN_FALLBACK
                for entry in params:
                    if entry.get("key") in ("getEnergyFee", "EnergyFee"):
                        v = entry.get("value")
                        if isinstance(v, int) and v > 0:
                            price = v
                            break
                self._energy_price_sun = price
                self._energy_price_fetched_at = now
                log.info("Chain energy price: %s sun/energy (cached %.0fs)",
                         price, _ENERGY_PRICE_TTL_SEC)
            except Exception as exc:
                if self._energy_price_sun is None:
                    log.warning(
                        "Failed to fetch chain energy price (%s): %s — using fallback %s",
                        type(exc).__name__, exc, TRON_ENERGY_PRICE_SUN_FALLBACK,
                    )
                    self._energy_price_sun = TRON_ENERGY_PRICE_SUN_FALLBACK
                    self._energy_price_fetched_at = now
                else:
                    log.warning(
                        "Failed to refresh chain energy price (%s): %s — keeping cached %s",
                        type(exc).__name__, exc, self._energy_price_sun,
                    )
                    self._energy_price_fetched_at = now
            return self._energy_price_sun

    # -- pre-flight energy estimate -----------------------------------------

    def estimate_transfer_energy(
        self, from_address: str, to_address: str, amount: Decimal,
    ) -> int | None:
        """Estimate energy required for a USDT transfer between two addresses.

        Address-parameterised so the auto-pick path can estimate from the
        chosen wallet, not a fixed singleton. Returns ``int`` or ``None`` if
        the node refuses to estimate (some endpoints disable this for cost
        reasons)."""
        amount_raw = int(amount * Decimal(10 ** USDT_DECIMALS))
        try:
            param = trx_abi.encode_single(
                "(address,uint256)", [to_address, amount_raw],
            ).hex()
        except Exception as exc:
            log.warning("Failed to encode transfer params for estimate: %s", exc)
            return None

        try:
            estimate = _with_retry(
                lambda: self.client.get_estimated_energy(
                    owner_address=from_address,
                    contract_address=USDT_CONTRACT,
                    function_selector="transfer(address,uint256)",
                    parameter=param,
                ),
                "get_estimated_energy",
            )
            energy = int(estimate or 0)
            if energy <= 0:
                log.warning(
                    "Energy estimate returned non-positive (%r) — falling back to fee_limit cap",
                    estimate,
                )
                return None
            return energy
        except Exception as exc:
            log.warning(
                "Energy estimate unavailable (%s): %s — falling back to fee_limit cap",
                type(exc).__name__, exc,
            )
            return None

    def compute_fee_limit_sun(self, estimated_energy: int | None) -> int:
        """Pick a per-tx fee_limit (SUN) based on the estimate.

        Bounded by [_MIN_FEE_LIMIT_SUN, USDT_FEE_LIMIT_SUN]. If no estimate
        is available, returns the configured ceiling — the node will refund
        any unused portion, so this is safe (we just lose tx-failure capping)."""
        if not estimated_energy:
            return USDT_FEE_LIMIT_SUN
        price = self.energy_price_sun()
        sun = int(Decimal(estimated_energy) * Decimal(price) * FEE_LIMIT_SAFETY_MULT)
        return max(_MIN_FEE_LIMIT_SUN, min(USDT_FEE_LIMIT_SUN, sun))

    # -- destination diagnostics --------------------------------------------

    def get_destination_info(self, to_address: str) -> dict:
        """Best-effort pre-flight info about the recipient.

        Diagnostic only — never blocks the send. Failures are logged and
        return safe defaults (exists=True, zero balances) so a flaky RPC
        can't kill an otherwise valid payout.

        Cold recipients (usdt_balance == 0) cost ~18k extra energy because
        the USDT contract initializes a fresh storage slot on first credit."""
        contract = self.get_usdt_contract()

        exists = True
        trx = Decimal(0)
        try:
            trx = self.client.get_account_balance(to_address)
        except Exception as exc:
            cls = type(exc).__name__.lower()
            if "not found" in str(exc).lower() or "addressnotfound" in cls:
                exists = False
            else:
                log.warning(
                    "Destination TRX query failed (%s): %s",
                    type(exc).__name__, exc,
                )

        usdt = Decimal(0)
        try:
            raw = contract.functions.balanceOf(to_address)
            usdt = Decimal(raw) / Decimal(10 ** USDT_DECIMALS)
        except Exception as exc:
            log.warning(
                "Destination USDT query failed (%s): %s",
                type(exc).__name__, exc,
            )

        return {"exists": exists, "trx_balance": trx, "usdt_balance": usdt}


# Singleton — one HTTP client pool process-wide
tron = TronClient()
