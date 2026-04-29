from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from skr_crypto.server.models import SendRequest


class TestSendRequest:
    def test_valid_request(self):
        req = SendRequest(
            to_address="T" + "A" * 33,
            amount=Decimal("100.50"),
            idempotency_key="key123",
        )
        assert req.amount == Decimal("100.50")

    def test_amount_is_decimal_not_float(self):
        req = SendRequest(
            to_address="T" + "A" * 33,
            amount="245.0572",
            idempotency_key="key123",
        )
        assert isinstance(req.amount, Decimal)

    def test_amount_zero_rejected(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            SendRequest(
                to_address="T" + "A" * 33,
                amount=Decimal("0"),
                idempotency_key="key123",
            )

    def test_amount_negative_rejected(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            SendRequest(
                to_address="T" + "A" * 33,
                amount=Decimal("-10"),
                idempotency_key="key123",
            )

    def test_amount_too_many_decimals(self):
        with pytest.raises(ValidationError, match="6 decimal"):
            SendRequest(
                to_address="T" + "A" * 33,
                amount=Decimal("1.1234567"),
                idempotency_key="key123",
            )

    def test_amount_6_decimals_ok(self):
        req = SendRequest(
            to_address="T" + "A" * 33,
            amount=Decimal("1.123456"),
            idempotency_key="key123",
        )
        assert req.amount == Decimal("1.123456")

    def test_address_too_short(self):
        with pytest.raises(ValidationError):
            SendRequest(
                to_address="Tshort",
                amount=Decimal("10"),
                idempotency_key="key123",
            )

    def test_address_too_long(self):
        with pytest.raises(ValidationError):
            SendRequest(
                to_address="T" + "A" * 34,
                amount=Decimal("10"),
                idempotency_key="key123",
            )

    def test_empty_idempotency_key(self):
        with pytest.raises(ValidationError):
            SendRequest(
                to_address="T" + "A" * 33,
                amount=Decimal("10"),
                idempotency_key="",
            )

    def test_idempotency_key_max_length(self):
        with pytest.raises(ValidationError):
            SendRequest(
                to_address="T" + "A" * 33,
                amount=Decimal("10"),
                idempotency_key="k" * 129,
            )
