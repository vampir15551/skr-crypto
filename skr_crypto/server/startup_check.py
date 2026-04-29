"""Startup self-check: reconcile recent audit with on-chain reality.

Run once at process startup, after the TronClient is ready. Reads the
durable audit log for the current MSK day and for every SEND_SUCCESS
record:

  - verifies the txid actually landed on-chain with a SUCCESS receipt
  - flags recipient addresses that received payouts more than once in
    the same MSK day (defence-in-depth — the idempotency key already
    rejects same `(wallet, day)` pairs, but two distinct keys to the
    same wallet would slip through and we want to know about that)

Design notes:

  - This is best-effort. Audit-file unavailability, RPC errors, malformed
    JSON — none of them block startup. The service will still come up.
  - Results are surfaced both via the operational logger (stdout / SIEM)
    and a single STARTUP_CHECK audit record so the durable trail
    captures the daily reconciliation.
  - "MSK window" is a fixed UTC+3 offset. Russia abolished DST in 2014
    so a static offset is correct — using `zoneinfo` would be overkill.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone

from skr_crypto.server import audit
from skr_crypto.server.config import AUDIT_LOG_FILE
from skr_crypto.server.tron_client import tron

log = logging.getLogger("payouts")

# MSK = UTC+3, no DST. Defines the operator's "today" boundary.
_MSK = timezone(timedelta(hours=3))


def _msk_today_start_utc() -> datetime:
    """Return midnight MSK *today* expressed in UTC, for window comparisons."""
    now_msk = datetime.now(_MSK)
    midnight_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_msk.astimezone(UTC)


def _read_audit_lines(path: str) -> list[dict]:
    """Parse the audit file as JSON-per-line.

    Returns an empty list on any read error — startup must continue. Bad
    individual lines are skipped (and logged) rather than aborting the
    whole parse, so a single corruption doesn't lose the rest of the day.
    """
    if not path or not os.path.exists(path):
        return []
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fp:
            for ln, raw in enumerate(fp, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError as exc:
                    log.warning(
                        "[STARTUP_CHECK] Audit line %d unparseable: %s",
                        ln, exc,
                    )
    except Exception as exc:
        log.warning("[STARTUP_CHECK] Failed to read audit file %s: %s", path, exc)
        return []
    return out


def _parse_iso(timestamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(timestamp)
    except Exception:
        return None


def _verify_txid_on_chain(txid: str) -> str:
    """Return the on-chain receipt code for `txid`.

    One of:
        SUCCESS         — landed and the contract call succeeded
        OUT_OF_ENERGY   — fee_limit hit, transfer reverted
        REVERT          — contract reverted (token rules / paused / blacklisted)
        <other>         — any other tronpy receipt code
        NOT_FOUND       — node has not seen the txid yet (or it never broadcast)
        RPC_ERROR       — look-up itself failed; status inconclusive

    NOT_FOUND vs RPC_ERROR is preserved so the operator can tell the
    difference between "tx is missing" and "we couldn't ask".
    """
    try:
        info = tron.client.get_transaction_info(txid)
    except Exception as exc:
        msg = str(exc).lower()
        if "not found" in msg or "transactionnotfound" in type(exc).__name__.lower():
            return "NOT_FOUND"
        log.warning("[STARTUP_CHECK] get_transaction_info(%s) failed: %s", txid, exc)
        return "RPC_ERROR"
    if not info:
        return "NOT_FOUND"
    receipt = info.get("receipt") or {}
    # tronpy omits "result" entirely when the receipt is SUCCESS — only
    # failure codes are included. Treat absence as success.
    return receipt.get("result") or "SUCCESS"


def run_startup_check() -> None:
    """Reconcile recent audit records with on-chain reality.

    Best-effort: any internal failure is logged, never raised. The
    service starts regardless.
    """
    if not AUDIT_LOG_FILE:
        log.info(
            "[STARTUP_CHECK] AUDIT_LOG_FILE not set — skipping reconciliation",
        )
        return

    window_start = _msk_today_start_utc()
    lines = _read_audit_lines(AUDIT_LOG_FILE)
    if not lines:
        log.info("[STARTUP_CHECK] No audit records to reconcile")
        return

    # Filter to today's MSK window, SEND_SUCCESS only, dedupe by txid.
    successes: dict[str, dict] = {}
    for entry in lines:
        if entry.get("event") != "SEND_SUCCESS":
            continue
        ts = _parse_iso(entry.get("timestamp", ""))
        if ts is None or ts < window_start:
            continue
        txid = entry.get("txid") or ""
        if not txid or txid in successes:
            continue
        successes[txid] = entry

    if not successes:
        log.info(
            "[STARTUP_CHECK] No SEND_SUCCESS records since %s — nothing to verify",
            window_start.isoformat(),
        )
        return

    log.info(
        "[STARTUP_CHECK] Reconciling %d txid(s) since %s",
        len(successes), window_start.isoformat(),
    )

    # ── Duplicate recipient detection ──────────────────────────────────
    # Idempotency key is `<wallet><date>` — same key, same day, same wallet
    # is already blocked. But if the operator (or a buggy client) generates
    # two *different* keys that both target the same wallet, both would
    # broadcast. That's what this check catches.
    by_recipient: dict[str, list[dict]] = defaultdict(list)
    for entry in successes.values():
        addr = entry.get("to_address") or ""
        if addr:
            by_recipient[addr].append(entry)

    duplicates = {addr: rs for addr, rs in by_recipient.items() if len(rs) > 1}
    if duplicates:
        for addr, rs in duplicates.items():
            txids = [r.get("txid", "")[:12] for r in rs]
            amounts = [r.get("amount", "?") for r in rs]
            log.warning(
                "[STARTUP_CHECK] DUPLICATE recipient | to=%s count=%d txids=%s amounts=%s",
                addr, len(rs), txids, amounts,
            )
    else:
        log.info("[STARTUP_CHECK] No duplicate recipients in MSK window")

    # ── On-chain status verification ───────────────────────────────────
    by_status: dict[str, int] = defaultdict(int)
    for txid, entry in successes.items():
        status = _verify_txid_on_chain(txid)
        by_status[status] += 1
        if status == "SUCCESS":
            log.info(
                "[STARTUP_CHECK] OK | txid=%s to=%s amount=%s",
                txid, entry.get("to_address", ""), entry.get("amount", ""),
            )
        else:
            log.warning(
                "[STARTUP_CHECK] %s | txid=%s to=%s amount=%s",
                status, txid, entry.get("to_address", ""), entry.get("amount", ""),
            )

    # Single audit summary so the durable trail captures the reconciliation.
    all_success = (
        not duplicates
        and by_status.get("SUCCESS", 0) == len(successes)
    )
    summary_details = (
        f"window_start={window_start.isoformat()} "
        f"checked={len(successes)} "
        f"by_status={dict(by_status)} "
        f"duplicates={len(duplicates)}"
    )
    audit.record(
        "STARTUP_CHECK",
        result=("ok" if all_success else "anomaly"),
        details=summary_details,
    )

    log.info("[STARTUP_CHECK] Done — %s", summary_details)
