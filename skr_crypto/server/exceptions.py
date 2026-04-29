from __future__ import annotations


class PayoutError(Exception):
    """Base exception for payout service."""

    def __init__(self, message: str, *, code: str = "PAYOUT_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


class InsufficientBalance(PayoutError):
    def __init__(self, asset: str, available: str, required: str):
        self.asset = asset
        self.available = available
        self.required = required
        super().__init__(
            f"Insufficient {asset}: have {available}, need {required}",
            code="INSUFFICIENT_BALANCE",
        )


class InvalidAddress(PayoutError):
    """Raised on a malformed TRON address.

    The full address is preserved in `self.address` for log/audit context,
    but the error message exposed to the API client truncates to 8 chars
    so a misclick doesn't echo the whole address back to a potentially
    different recipient.
    """

    def __init__(self, address: str, reason: str = "invalid format"):
        self.address = address
        # Public message: truncated. Logs/audit can read self.address for full.
        super().__init__(
            f"Invalid address {address[:8]}...: {reason}",
            code="INVALID_ADDRESS",
        )


class TransactionFailed(PayoutError):
    def __init__(self, reason: str):
        super().__init__(f"Transaction failed: {reason}", code="TX_FAILED")


class EnergyTooExpensive(PayoutError):
    """Raised when the on-chain energy estimate would burn more TRX than
    MAX_ENERGY_BURN_TRX allows. The operator should stake TRX or rent
    energy from a marketplace instead."""

    def __init__(self, estimated_energy: int, estimated_burn_trx, max_burn_trx):
        self.estimated_energy = estimated_energy
        self.estimated_burn_trx = str(estimated_burn_trx)
        self.max_burn_trx = str(max_burn_trx)
        super().__init__(
            f"Estimated energy burn ({estimated_burn_trx} TRX) exceeds limit "
            f"({max_burn_trx} TRX) — stake TRX or rent energy",
            code="ENERGY_TOO_EXPENSIVE",
        )


class RiskTooHigh(PayoutError):
    """Recipient address tripped the wallet-risk preflight at or above
    the configured ``RISK_BLOCK_LEVEL``.

    The full report is attached as ``self.report`` (a dict) so the
    error handler can surface the failed checks back to the client.
    Operators inspect ``skr-crypto risk <addr>`` for the same data.
    """

    def __init__(self, level: str, report: dict):
        self.level = level
        self.report = report
        failed = [
            c["name"]
            for c in report.get("checks", [])
            if c.get("status") == "fail"
        ]
        detail = "; ".join(failed) if failed else "no failed checks"
        super().__init__(
            f"Recipient risk {level.upper()} — refusing to broadcast. Failed: {detail}",
            code="RISK_TOO_HIGH",
        )
