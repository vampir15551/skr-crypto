"""Single TRON wallet — key + address + signing.

A ``Wallet`` is the bind between a name (operator-friendly label) and
the cryptographic material needed to spend from one address. We keep
this object thin on purpose:

  - It does NOT own the HTTP/RPC client (that's the keyless ``TronClient``
    singleton, shared across all wallets).
  - It does NOT cache balances or contract handles (the RPC client does,
    once, for the USDT contract; balance reads always hit the chain).
  - It DOES own the ``PrivateKey`` and is the one place that calls
    ``.sign()``. Every signing path goes through ``Wallet.send_usdt``.

Why split ``Wallet`` from the RPC client:

  - Multi-wallet treasuries: one HTTP connection pool, N signing keys.
  - Tests can construct a fake ``Wallet`` without monkey-patching the
    singleton. (``tests/conftest.py`` still mocks the RPC layer because
    we don't want network in tests.)
  - The blast radius of any signing-related change is bounded to one
    file.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from tronpy.keys import PrivateKey

from skr_crypto.server.config import USDT_DECIMALS, USDT_FEE_LIMIT_SUN

log = logging.getLogger("payouts")

# Mirrors the floor in the legacy TronClient. Even if the estimate says
# "tiny", we never go below this — node sometimes spikes energy on
# contract state changes.
_MIN_FEE_LIMIT_SUN: int = 5_000_000  # 5 TRX


@dataclass
class Wallet:
    """One key + address + display name.

    ``priv_key`` is held until ``destroy()`` is called at process exit.
    See ``security.py`` for the honest disclosure on what wiping bytes
    in CPython actually buys you.
    """

    name: str
    address: str
    priv_key: PrivateKey | None = field(repr=False)

    # ----- signing -----------------------------------------------------------

    def send_usdt(
        self,
        client,                      # tronpy.Tron — passed in to avoid back-import
        usdt_contract,               # contract handle from TronClient cache
        to_address: str,
        amount: Decimal,
        fee_limit_sun: int | None = None,
    ) -> str:
        """Build, sign, and broadcast a USDT TRC-20 transfer.

        Returns the txid on success, or raises ``RuntimeError`` with a
        diagnostic message on broadcast rejection. NEVER retries — a
        retry on broadcast risks double-spend.
        """
        if self.priv_key is None:
            raise RuntimeError(f"wallet {self.name!r}: priv_key already destroyed")

        amount_raw = int(amount * Decimal(10 ** USDT_DECIMALS))

        # fee_limit is a CAP on burn — actual cost is energy_used * price.
        # Lower cap = bounded loss if estimate is wrong. Higher cap = tx
        # survives unexpected energy spikes. We pick min(estimate*1.3, ceiling).
        if fee_limit_sun is None:
            fee_limit_sun = USDT_FEE_LIMIT_SUN
        fee_limit_sun = max(_MIN_FEE_LIMIT_SUN, min(USDT_FEE_LIMIT_SUN, fee_limit_sun))

        txn = (
            usdt_contract.functions.transfer(to_address, amount_raw)
            .with_owner(self.address)
            .fee_limit(fee_limit_sun)
            .build()
            .sign(self.priv_key)
        )
        # NO retry on broadcast — duplicate tx = double spend
        result = txn.broadcast()

        # Always log the raw response so post-mortem diagnostics see exactly
        # what the node returned — including fields beyond result/code/message
        # /txid (e.g. SIGERROR with a partial txid).
        log.info("[wallet=%s] Broadcast response: %r", self.name, result)

        if not isinstance(result, dict):
            raise RuntimeError(
                f"unexpected broadcast response type: {type(result).__name__}"
            )

        if result.get("result") is not True:
            code = result.get("code") or "UNKNOWN"
            message = result.get("message") or ""
            raise RuntimeError(
                f"broadcast rejected: code={code} message={message!r}"
            )

        txid = result.get("txid") or ""
        if not txid:
            raise RuntimeError(f"broadcast returned empty txid: {result!r}")

        log.info(
            "[wallet=%s] TX sent: %s -> %s amount=%s USDT txid=%s fee_limit=%s sun",
            self.name, self.address, to_address, amount, txid, fee_limit_sun,
        )
        return txid

    # ----- lifecycle ---------------------------------------------------------

    def destroy(self) -> None:
        """Drop the private key reference. Best-effort — see security.py."""
        self.priv_key = None
