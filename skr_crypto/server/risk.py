"""Wallet-risk assessment for TRON USDT recipients.

Two tiers, both exposed through one entry point :func:`assess_risk`:

  - **Tier 1 — local, free, fast** (always run): base58check validity,
    known-burn-address pattern check, on-chain activation
    (``get_account``), smart-contract destination detection
    (``get_contract``), Tether USDT blacklist (``isBlackListed`` view
    on the USDT TRC-20 contract), and a balance / activity sketch.
    ~3-4 RPC calls; runs against your existing TronGrid endpoint.

  - **Tier 2 — external, opt-in** (``external=True``): TronScan
    public security endpoint reports tags / blacklist flags maintained
    by tronscan.org. Adds one HTTP call (~200-500ms). Best-effort —
    if TronScan is unreachable the check returns ``SKIP`` instead of
    failing.

The whole module is sync-only; no asyncio touches the money path
(see ``docs/adr/0001-sync-only-architecture.md``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import base58
import requests

log = logging.getLogger("payouts")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class RiskLevel(StrEnum):
    """Coarse-grained verdict. Drives /send blocking + CLI exit codes.

    Mapping: any high-severity FAIL → HIGH; any FAIL or WARN → MEDIUM;
    everything OK → LOW; address fails validity → INVALID.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    INVALID = "invalid"


class CheckStatus(StrEnum):
    """Per-check outcome.

    - OK: passed.
    - WARN: informational issue, may bump level to MEDIUM but never HIGH.
    - FAIL: real problem; ``severity == "high"`` bumps the level to HIGH.
    - SKIP: not run (no network, RPC error, opt-in not chosen). Not a bug.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class RiskCheck:
    """One check's result. Surfaced verbatim in the JSON API."""

    name: str
    status: CheckStatus
    message: str
    # "info" | "warn" | "high" — controls how this contributes to RiskLevel.
    severity: str = "info"


@dataclass
class RiskReport:
    address: str
    level: RiskLevel
    checks: list[RiskCheck]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "level": self.level.value,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "message": c.message,
                    "severity": c.severity,
                }
                for c in self.checks
            ],
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Known burn / null addresses on TRON.
# ---------------------------------------------------------------------------
#
# These are addresses any reasonable operator would be heartbroken to
# send USDT to. The list below is the obvious one ("TRON null"); the
# zero-body pattern check below catches any other address whose 20-byte
# body decodes to all zeros.

KNOWN_BURN_ADDRESSES = frozenset({
    "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",  # TRON null / "0x0" address
})


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def assess_risk(address: str, *, external: bool = False) -> RiskReport:
    """Run all risk checks against ``address``. Returns a ``RiskReport``.

    Validity is checked first; an invalid address short-circuits the
    rest (we don't want to call TronGrid on garbage input). All other
    checks are best-effort: an RPC failure on one of them shows up as
    ``SKIP`` and doesn't poison the whole result.
    """
    # 0. Validity — must succeed before anything else.
    validity = _check_validity(address)
    if validity.status is CheckStatus.FAIL:
        return RiskReport(
            address=address,
            level=RiskLevel.INVALID,
            checks=[validity],
            summary={},
        )

    checks: list[RiskCheck] = [validity]
    summary: dict[str, Any] = {}

    # 1. Burn-pattern (purely local, no network).
    checks.append(_check_burn_address(address))

    # 2-5. Local on-chain checks.
    from skr_crypto.server.tron_client import tron

    activation, account = _check_activation(tron, address)
    checks.append(activation)
    if account:
        if account.get("create_time"):
            summary["create_time_ms"] = int(account["create_time"])
        if account.get("latest_opration_time"):
            summary["latest_opration_time_ms"] = int(account["latest_opration_time"])

    checks.append(_check_smart_contract(tron, address))
    checks.append(_check_usdt_blacklist(tron, address))

    balance, balance_summary = _check_balance(tron, address)
    checks.append(balance)
    summary.update(balance_summary)

    # 6. External (opt-in).
    if external:
        checks.append(_check_external_tronscan(address))

    return RiskReport(
        address=address,
        level=_level_from(checks),
        checks=checks,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_validity(address: str) -> RiskCheck:
    """Same rules as ``app.routes._validate_tron_address``: T-prefix +
    base58check + 21 bytes total + 0x41 leading byte."""
    if not isinstance(address, str) or not address.startswith("T"):
        return RiskCheck("validity", CheckStatus.FAIL,
                         "address must start with T", "high")
    try:
        decoded = base58.b58decode_check(address)
    except Exception:
        return RiskCheck("validity", CheckStatus.FAIL,
                         "invalid base58check encoding", "high")
    if len(decoded) != 21:
        return RiskCheck("validity", CheckStatus.FAIL,
                         f"decoded length {len(decoded)} != 21 bytes", "high")
    if decoded[0] != 0x41:
        return RiskCheck("validity", CheckStatus.FAIL,
                         "wrong TRON prefix byte", "high")
    return RiskCheck("validity", CheckStatus.OK, "valid TRON base58 address")


def _check_burn_address(address: str) -> RiskCheck:
    if address in KNOWN_BURN_ADDRESSES:
        return RiskCheck(
            "burn_address",
            CheckStatus.FAIL,
            "address is on the known burn / null list",
            "high",
        )
    # Catch any address whose 20-byte body decodes to all zeros.
    try:
        decoded = base58.b58decode_check(address)
        if all(b == 0 for b in decoded[1:]):
            return RiskCheck(
                "burn_address",
                CheckStatus.FAIL,
                "address decodes to a zero-body pattern (functional burn)",
                "high",
            )
    except Exception:
        # If validity already passed, decode shouldn't fail. But be
        # defensive — never crash a risk check.
        pass
    return RiskCheck("burn_address", CheckStatus.OK,
                     "not a known burn pattern")


def _check_activation(tron, address: str) -> tuple[RiskCheck, dict | None]:
    """``get_account`` on an unactivated address returns either an empty
    dict or raises (depending on tronpy version). Both are treated as
    "not activated" — a USDT transfer will likely succeed but the
    recipient can't move the funds without first activating the account
    (e.g. by receiving TRX)."""
    try:
        account = tron.client.get_account(address)
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "addressnotfound" in type(exc).__name__.lower():
            return (
                RiskCheck(
                    "activation",
                    CheckStatus.WARN,
                    "address NOT activated on chain — recipient must activate "
                    "(receive a small TRX) before they can move the USDT",
                    "warn",
                ),
                None,
            )
        return (
            RiskCheck("activation", CheckStatus.SKIP,
                      f"could not query account: {type(exc).__name__}"),
            None,
        )

    if not account or not account.get("create_time"):
        return (
            RiskCheck(
                "activation",
                CheckStatus.WARN,
                "address NOT activated on chain (account record empty)",
                "warn",
            ),
            account,
        )
    return (
        RiskCheck("activation", CheckStatus.OK, "activated on chain"),
        account,
    )


def _check_smart_contract(tron, address: str) -> RiskCheck:
    """If ``get_contract`` returns a contract object, the address has
    bytecode. Sending USDT to a contract that doesn't implement TRC-20
    transfer-receiving logic locks the funds. We can't tell from
    bytecode whether the contract handles incoming USDT — the safe
    default is to flag.

    Note: tronpy's ``get_contract`` raises (rather than returning None)
    when the address is not a contract. We catch broadly."""
    try:
        contract = tron.client.get_contract(address)
    except Exception:
        return RiskCheck("smart_contract", CheckStatus.OK,
                         "not a smart contract")
    # Conservative: any non-falsy contract object means there's bytecode
    # at this address. We don't try to inspect its ABI here.
    if contract:
        return RiskCheck(
            "smart_contract",
            CheckStatus.FAIL,
            "destination is a smart contract — sending USDT to a contract "
            "that does not handle TRC-20 incoming transfers will lock the "
            "funds. Verify recipient supports TRC-20 receive.",
            "high",
        )
    return RiskCheck("smart_contract", CheckStatus.OK, "not a smart contract")


def _check_usdt_blacklist(tron, address: str) -> RiskCheck:
    """Tether maintains an on-chain blacklist on the USDT TRC-20
    contract via ``isBlackListed(address)``. If the destination is
    blacklisted, the broadcast will succeed but the on-chain transfer
    reverts and energy / fee_limit is consumed.

    The most-bang-for-buck check in this module: catches a real,
    reproducible class of "broadcast OK, money silently didn't move"
    failures that operators have hit IRL.
    """
    try:
        contract = tron._get_usdt_contract()
        is_blacklisted = contract.functions.isBlackListed(address)
    except Exception as exc:
        return RiskCheck(
            "usdt_blacklist",
            CheckStatus.SKIP,
            f"could not query Tether blacklist: {type(exc).__name__}",
        )
    if is_blacklisted:
        return RiskCheck(
            "usdt_blacklist",
            CheckStatus.FAIL,
            "address is on Tether's USDT blacklist — broadcast WILL succeed "
            "but on-chain transfer will REVERT and burn fee_limit",
            "high",
        )
    return RiskCheck("usdt_blacklist", CheckStatus.OK,
                     "not on Tether blacklist")


def _check_balance(tron, address: str) -> tuple[RiskCheck, dict[str, Any]]:
    """Use the existing ``get_destination_info`` helper — same call the
    /send hot path makes for warm/cold detection."""
    try:
        info = tron.get_destination_info(address)
    except Exception as exc:
        return (
            RiskCheck(
                "balance",
                CheckStatus.SKIP,
                f"could not query balance: {type(exc).__name__}",
            ),
            {},
        )
    summary = {
        "trx_balance": str(info.get("trx_balance", 0)),
        "usdt_balance": str(info.get("usdt_balance", 0)),
        "warmth": "cold" if info.get("usdt_balance", 0) == 0 else "warm",
    }
    msg = (
        f"recipient holds {info.get('usdt_balance', 0)} USDT, "
        f"{info.get('trx_balance', 0)} TRX"
    )
    if summary["warmth"] == "cold":
        msg += " (USDT cold — first transfer will need ~32k energy)"
    return RiskCheck("balance", CheckStatus.OK, msg), summary


def _check_external_tronscan(
    address: str, *, timeout: float = 5.0,
) -> RiskCheck:
    """Tier 2: TronScan reputation lookup.

    TronScan exposes a public security endpoint that aggregates
    community / curated blacklist data. Best-effort: any network
    failure shows as SKIP.

    We're querying tronscan.org directly — that's the same source
    operators read by hand when debugging anyway. No API key needed.
    """
    url = (
        "https://apilist.tronscanapi.com/api/security/account/data"
        f"?address={address}"
    )
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "skr-crypto-risk/1"},
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return RiskCheck(
            "external_tronscan",
            CheckStatus.SKIP,
            f"TronScan unreachable: {type(exc).__name__}",
        )

    flags: list[str] = []
    # Top-level black flag
    if data.get("isBlack"):
        flags.append("flagged isBlack")
    # Per-source list entries
    items = data.get("list") or []
    if isinstance(items, list):
        for entry in items:
            if isinstance(entry, dict) and entry.get("black"):
                kind = entry.get("blackType") or entry.get("source") or "unknown"
                flags.append(f"blacklisted ({kind})")
    # Account-level tags (sometimes the field name differs by version)
    tags = data.get("redTag") or data.get("scamTag")
    if tags:
        flags.append(f"red-tagged: {tags}")

    if flags:
        return RiskCheck(
            "external_tronscan",
            CheckStatus.FAIL,
            "TronScan flags: " + "; ".join(flags),
            "high",
        )
    return RiskCheck(
        "external_tronscan",
        CheckStatus.OK,
        "no TronScan flags",
    )


# ---------------------------------------------------------------------------
# Level computation
# ---------------------------------------------------------------------------


def _level_from(checks: list[RiskCheck]) -> RiskLevel:
    has_high_fail = any(
        c.status is CheckStatus.FAIL and c.severity == "high" for c in checks
    )
    has_warn = any(c.status is CheckStatus.WARN for c in checks)
    has_fail = any(c.status is CheckStatus.FAIL for c in checks)

    if has_high_fail:
        return RiskLevel.HIGH
    if has_fail or has_warn:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


# ---------------------------------------------------------------------------
# /send preflight helper
# ---------------------------------------------------------------------------


def should_block(level: RiskLevel, block_at: str) -> bool:
    """Translate ``RISK_BLOCK_LEVEL`` config into a decision.

    ``block_at`` is one of ``high`` / ``medium`` / ``none``. ``none``
    disables blocking entirely (everything is allowed through; the
    operator still sees the report in the audit). HIGH-fail at level
    medium or high blocks; INVALID always blocks (validity is a
    separate failure mode handled by ``_validate_tron_address`` upstream).
    """
    block_at = block_at.lower()
    if block_at == "none":
        return False
    if level is RiskLevel.HIGH or level is RiskLevel.INVALID:
        return True
    if block_at == "medium" and level is RiskLevel.MEDIUM:
        return True
    return False
