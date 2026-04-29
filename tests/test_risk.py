"""Unit tests for ``skr_crypto.server.risk``.

The TronClient singleton is patched with ``mock_tron`` so no real
RPCs leave the test process. TronScan calls (Tier 2) are stubbed via
``responses``.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import responses

from skr_crypto.server.risk import (
    CheckStatus,
    RiskLevel,
    assess_risk,
    should_block,
)

VALID = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"
NULL_TRON = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"


# ---------------------------------------------------------------------------
# Validity short-circuit
# ---------------------------------------------------------------------------


class TestValidity:
    def test_invalid_short_circuits(self, mock_tron):
        report = assess_risk("not-an-address")
        assert report.level is RiskLevel.INVALID
        # Only the validity check ran — no RPCs were made.
        names = [c.name for c in report.checks]
        assert names == ["validity"]
        assert report.checks[0].status is CheckStatus.FAIL

    def test_invalid_prefix_byte(self, mock_tron):
        # Hand-craft an address with the wrong leading byte.
        # Decoded TRON addresses are 21 bytes prefixed with 0x41.
        # Any well-formed base58check that doesn't start with T fails
        # the .startswith("T") check first.
        report = assess_risk("XYZdeadbeef")
        assert report.level is RiskLevel.INVALID

    def test_empty_string(self, mock_tron):
        report = assess_risk("")
        assert report.level is RiskLevel.INVALID

    def test_valid_address_runs_full_battery(self, mock_tron):
        # Make every downstream check pass cleanly.
        mock_tron.client.get_account = MagicMock(return_value={"create_time": 1700000000_000})
        mock_tron.client.get_contract = MagicMock(side_effect=Exception("not a contract"))
        mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(return_value=False)
        mock_tron.get_destination_info = MagicMock(return_value={
            "exists": True, "trx_balance": Decimal("1"), "usdt_balance": Decimal("100"),
        })
        report = assess_risk(VALID)
        assert report.level is RiskLevel.LOW
        names = [c.name for c in report.checks]
        for n in ("validity", "burn_address", "activation",
                  "smart_contract", "usdt_blacklist", "balance"):
            assert n in names


# ---------------------------------------------------------------------------
# Burn-address detection
# ---------------------------------------------------------------------------


class TestBurnAddress:
    def test_known_null_blocks_high(self, mock_tron):
        # We need the rest of the checks to behave; mock generously.
        mock_tron.client.get_account = MagicMock(return_value={})
        mock_tron.client.get_contract = MagicMock(side_effect=Exception())
        mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(return_value=False)
        mock_tron.get_destination_info = MagicMock(return_value={
            "exists": False, "trx_balance": Decimal("0"), "usdt_balance": Decimal("0"),
        })
        report = assess_risk(NULL_TRON)
        burn = next(c for c in report.checks if c.name == "burn_address")
        assert burn.status is CheckStatus.FAIL
        assert burn.severity == "high"
        assert report.level is RiskLevel.HIGH


# ---------------------------------------------------------------------------
# USDT blacklist
# ---------------------------------------------------------------------------


class TestUsdtBlacklist:
    def _setup_clean(self, mock_tron):
        mock_tron.client.get_account = MagicMock(return_value={"create_time": 1700000000_000})
        mock_tron.client.get_contract = MagicMock(side_effect=Exception())
        mock_tron.get_destination_info = MagicMock(return_value={
            "exists": True, "trx_balance": Decimal("1"), "usdt_balance": Decimal("0"),
        })

    def test_blacklisted_blocks_high(self, mock_tron):
        self._setup_clean(mock_tron)
        mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(
            return_value=True,
        )
        report = assess_risk(VALID)
        bl = next(c for c in report.checks if c.name == "usdt_blacklist")
        assert bl.status is CheckStatus.FAIL
        assert bl.severity == "high"
        assert report.level is RiskLevel.HIGH

    def test_rpc_failure_skips_not_fails(self, mock_tron):
        self._setup_clean(mock_tron)
        mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(
            side_effect=RuntimeError("503 from TronGrid"),
        )
        report = assess_risk(VALID)
        bl = next(c for c in report.checks if c.name == "usdt_blacklist")
        assert bl.status is CheckStatus.SKIP
        # No FAIL → no HIGH from blacklist; level stays LOW or MEDIUM
        # depending on activation/balance flags.
        assert report.level is not RiskLevel.HIGH


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


class TestActivation:
    def _setup_clean(self, mock_tron):
        mock_tron.client.get_contract = MagicMock(side_effect=Exception())
        mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(return_value=False)
        mock_tron.get_destination_info = MagicMock(return_value={
            "exists": False, "trx_balance": Decimal("0"), "usdt_balance": Decimal("0"),
        })

    def test_unactivated_address_warns_medium(self, mock_tron):
        self._setup_clean(mock_tron)
        # The check looks for "not found" in message OR "addressnotfound"
        # in the exception class name. Match the message form here.
        mock_tron.client.get_account = MagicMock(
            side_effect=Exception("Address not found on chain"),
        )
        report = assess_risk(VALID)
        act = next(c for c in report.checks if c.name == "activation")
        assert act.status is CheckStatus.WARN
        assert report.level is RiskLevel.MEDIUM

    def test_empty_account_record_warns_medium(self, mock_tron):
        """tronpy sometimes returns an empty dict for unactivated
        addresses instead of raising. Treat that the same as 'not
        activated'."""
        self._setup_clean(mock_tron)
        mock_tron.client.get_account = MagicMock(return_value={})
        report = assess_risk(VALID)
        act = next(c for c in report.checks if c.name == "activation")
        assert act.status is CheckStatus.WARN
        assert report.level is RiskLevel.MEDIUM


# ---------------------------------------------------------------------------
# Smart-contract destination
# ---------------------------------------------------------------------------


class TestSmartContract:
    def _setup_clean(self, mock_tron):
        mock_tron.client.get_account = MagicMock(return_value={"create_time": 1700000000_000})
        mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(return_value=False)
        mock_tron.get_destination_info = MagicMock(return_value={
            "exists": True, "trx_balance": Decimal("1"), "usdt_balance": Decimal("0"),
        })

    def test_contract_destination_blocks_high(self, mock_tron):
        self._setup_clean(mock_tron)
        mock_tron.client.get_contract = MagicMock(return_value={"name": "SomeContract"})
        report = assess_risk(VALID)
        sc = next(c for c in report.checks if c.name == "smart_contract")
        assert sc.status is CheckStatus.FAIL
        assert sc.severity == "high"
        assert report.level is RiskLevel.HIGH


# ---------------------------------------------------------------------------
# External (Tier 2) — TronScan
# ---------------------------------------------------------------------------


class TestExternalTronScan:
    def _setup_clean(self, mock_tron):
        mock_tron.client.get_account = MagicMock(return_value={"create_time": 1700000000_000})
        mock_tron.client.get_contract = MagicMock(side_effect=Exception())
        mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(return_value=False)
        mock_tron.get_destination_info = MagicMock(return_value={
            "exists": True, "trx_balance": Decimal("1"), "usdt_balance": Decimal("0"),
        })

    @responses.activate
    def test_external_clean_passes(self, mock_tron):
        self._setup_clean(mock_tron)
        responses.add(
            responses.GET,
            f"https://apilist.tronscanapi.com/api/security/account/data?address={VALID}",
            json={"isBlack": False, "list": []},
            status=200,
        )
        report = assess_risk(VALID, external=True)
        ts = next(c for c in report.checks if c.name == "external_tronscan")
        assert ts.status is CheckStatus.OK

    @responses.activate
    def test_external_flags_block_high(self, mock_tron):
        self._setup_clean(mock_tron)
        responses.add(
            responses.GET,
            f"https://apilist.tronscanapi.com/api/security/account/data?address={VALID}",
            json={"isBlack": True, "list": [{"black": True, "blackType": "scam"}]},
            status=200,
        )
        report = assess_risk(VALID, external=True)
        ts = next(c for c in report.checks if c.name == "external_tronscan")
        assert ts.status is CheckStatus.FAIL
        assert ts.severity == "high"
        assert report.level is RiskLevel.HIGH

    @responses.activate
    def test_external_unreachable_skips(self, mock_tron):
        self._setup_clean(mock_tron)
        responses.add(
            responses.GET,
            f"https://apilist.tronscanapi.com/api/security/account/data?address={VALID}",
            body="bzzzt", status=502,
        )
        report = assess_risk(VALID, external=True)
        ts = next(c for c in report.checks if c.name == "external_tronscan")
        assert ts.status is CheckStatus.SKIP
        # SKIP doesn't add to level.
        assert report.level is not RiskLevel.HIGH

    def test_external_skipped_when_flag_off(self, mock_tron):
        self._setup_clean(mock_tron)
        report = assess_risk(VALID, external=False)
        names = [c.name for c in report.checks]
        assert "external_tronscan" not in names


# ---------------------------------------------------------------------------
# should_block helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level,block_at,expected", [
    (RiskLevel.LOW,     "high",   False),
    (RiskLevel.LOW,     "medium", False),
    (RiskLevel.LOW,     "none",   False),
    (RiskLevel.MEDIUM,  "high",   False),
    (RiskLevel.MEDIUM,  "medium", True),
    (RiskLevel.MEDIUM,  "none",   False),
    (RiskLevel.HIGH,    "high",   True),
    (RiskLevel.HIGH,    "medium", True),
    (RiskLevel.HIGH,    "none",   False),
    (RiskLevel.INVALID, "high",   True),
    (RiskLevel.INVALID, "medium", True),
    (RiskLevel.INVALID, "none",   False),
])
def test_should_block(level, block_at, expected):
    assert should_block(level, block_at) is expected


# ---------------------------------------------------------------------------
# Report serialisation
# ---------------------------------------------------------------------------


def test_report_to_dict_shape(mock_tron):
    mock_tron.client.get_account = MagicMock(return_value={"create_time": 1700000000_000})
    mock_tron.client.get_contract = MagicMock(side_effect=Exception())
    mock_tron._get_usdt_contract().functions.isBlackListed = MagicMock(return_value=False)
    mock_tron.get_destination_info = MagicMock(return_value={
        "exists": True, "trx_balance": Decimal("1"), "usdt_balance": Decimal("0"),
    })
    report = assess_risk(VALID)
    payload = report.to_dict()
    assert payload["address"] == VALID
    assert payload["level"] in ("low", "medium", "high", "invalid")
    assert isinstance(payload["checks"], list)
    for c in payload["checks"]:
        assert {"name", "status", "message", "severity"} <= set(c)
    assert isinstance(payload["summary"], dict)
