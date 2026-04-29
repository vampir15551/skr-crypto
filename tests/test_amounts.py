from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from skr_crypto.server.models import SendRequest
from skr_crypto.server.tron_client import USDT_DECIMALS

ADDR = "T" + "A" * 33


class TestAmountEdgeCases:
    """Boundary and edge-case tests for monetary amounts."""

    def test_minimum_usdt(self):
        """Smallest possible USDT amount: 0.000001 (1 sun)."""
        req = SendRequest(to_address=ADDR, amount=Decimal("0.000001"), idempotency_key="k")
        assert req.amount == Decimal("0.000001")

    def test_one_sun_raw_conversion(self):
        """0.000001 USDT * 10^6 = 1 (one sun in raw units)."""
        amount = Decimal("0.000001")
        raw = int(amount * Decimal(10 ** USDT_DECIMALS))
        assert raw == 1

    def test_large_amount(self):
        """Large but valid amount — 10 million USDT."""
        req = SendRequest(to_address=ADDR, amount=Decimal("10000000"), idempotency_key="k")
        raw = int(req.amount * Decimal(10 ** USDT_DECIMALS))
        assert raw == 10_000_000_000_000

    def test_large_amount_raw_fits_uint256(self):
        """Even huge amounts must fit in uint256 (max 2^256-1)."""
        amount = Decimal("999999999999")  # ~1 trillion USDT
        raw = int(amount * Decimal(10 ** USDT_DECIMALS))
        assert raw < 2**256

    def test_7_decimals_rejected(self):
        with pytest.raises(ValidationError, match="6 decimal"):
            SendRequest(to_address=ADDR, amount=Decimal("1.0000001"), idempotency_key="k")

    def test_18_decimals_rejected(self):
        with pytest.raises(ValidationError, match="6 decimal"):
            SendRequest(to_address=ADDR, amount=Decimal("1.123456789012345678"), idempotency_key="k")

    def test_exact_6_decimals(self):
        req = SendRequest(to_address=ADDR, amount=Decimal("1.123456"), idempotency_key="k")
        raw = int(req.amount * Decimal(10 ** USDT_DECIMALS))
        assert raw == 1_123_456

    def test_integer_amount(self):
        req = SendRequest(to_address=ADDR, amount=Decimal("500"), idempotency_key="k")
        raw = int(req.amount * Decimal(10 ** USDT_DECIMALS))
        assert raw == 500_000_000

    def test_trailing_zeros_ok(self):
        """1.10 should be fine — only 2 decimal places."""
        req = SendRequest(to_address=ADDR, amount=Decimal("1.10"), idempotency_key="k")
        assert req.amount == Decimal("1.10")

    def test_very_small_rejected(self):
        """Below 1 sun — 7 decimal places."""
        with pytest.raises(ValidationError, match="6 decimal"):
            SendRequest(to_address=ADDR, amount=Decimal("0.0000001"), idempotency_key="k")

    def test_raw_no_precision_loss(self):
        """Decimal multiplication must not lose precision."""
        amounts = [
            Decimal("0.000001"),
            Decimal("0.1"),
            Decimal("1.23456"),
            Decimal("99999.999999"),
            Decimal("100000"),
        ]
        for amt in amounts:
            raw = int(amt * Decimal(10 ** USDT_DECIMALS))
            back = Decimal(raw) / Decimal(10 ** USDT_DECIMALS)
            assert back == amt, f"Precision loss for {amt}: raw={raw}, back={back}"
