"""``skr-crypto risk <address>`` — wallet-risk look-up.

Read-only: hits the service's ``GET /api/v1/risk/{address}`` endpoint
and prints a verdict + per-check breakdown. Exit code reflects the
verdict so the operator can chain it::

    skr-crypto risk $ADDR && some-other-step

  0   LOW     all checks green
  10  MEDIUM  warnings or non-high-severity fails
  11  HIGH    blocking finding (Tether blacklist, smart contract,
              burn pattern) — a /send to the same address would be
              refused by the server unless RISK_BLOCK_LEVEL=none
  12  INVALID address didn't validate at all
"""
from __future__ import annotations

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.api import APIClient
from skr_crypto.cli.exceptions import (
    RISK_EXIT_HIGH,
    RISK_EXIT_INVALID,
    RISK_EXIT_LOW,
    RISK_EXIT_MEDIUM,
)


@click.command("risk")
@click.argument("address")
@click.option(
    "--external/--no-external",
    default=False,
    help="Also query the external reputation source (TronScan). "
         "Adds ~300ms. The service-side `/send` preflight is controlled "
         "separately by RISK_USE_EXTERNAL in .env.",
)
@click.option("--json", "as_json", is_flag=True,
              help="Emit the raw report as JSON to stdout.")
@click.pass_context
def cmd(ctx: click.Context, address: str, external: bool, as_json: bool) -> None:
    """Risk-check a TRON address before sending money to it.

    Runs all server-side checks: validity, known-burn pattern, on-chain
    activation, smart-contract destination, Tether USDT blacklist,
    balance / activity sketch. ``--external`` adds a TronScan
    reputation lookup.
    """
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    client = APIClient.from_env_file(install_dir / ".env", timeout=30)
    report = client.risk(address, external=external)

    if as_json:
        output.print_json(report)
    else:
        _render_human(report)

    # Exit code per level — useful in shell pipelines.
    level = (report.get("level") or "").lower()
    if level == "low":
        ctx.exit(RISK_EXIT_LOW)
    if level == "medium":
        ctx.exit(RISK_EXIT_MEDIUM)
    if level == "high":
        ctx.exit(RISK_EXIT_HIGH)
    if level == "invalid":
        ctx.exit(RISK_EXIT_INVALID)
    # Unknown level — fall through with code 1 so something obviously
    # went wrong server-side.
    ctx.exit(1)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _render_human(report: dict) -> None:
    addr = report.get("address", "?")
    level = (report.get("level") or "?").lower()
    summary = report.get("summary", {}) or {}
    checks = report.get("checks", []) or []

    output.print_value(f"address:    [bold]{addr}[/bold]")

    # Activity / balance summary line.
    if summary:
        bits = []
        if "trx_balance" in summary:
            bits.append(f"TRX={summary['trx_balance']}")
        if "usdt_balance" in summary:
            bits.append(f"USDT={summary['usdt_balance']}")
        if "warmth" in summary:
            bits.append(f"warmth={summary['warmth']}")
        if bits:
            output.print_value("balance:    " + "  ".join(bits))

    # Per-check table.
    table = output.make_table(
        "check", "status", "detail", title=None,
    )
    for c in checks:
        status = (c.get("status") or "?").lower()
        colour = {
            "ok":   "green",
            "warn": "yellow",
            "fail": "red",
            "skip": "dim",
        }.get(status, "white")
        table.add_row(
            str(c.get("name", "?")),
            f"[{colour}]{status.upper()}[/{colour}]",
            str(c.get("message", "")),
        )
    output.render_table(table)

    # Verdict line.
    verdict_colour = {
        "low":     "green",
        "medium":  "yellow",
        "high":    "red",
        "invalid": "red",
    }.get(level, "white")
    output.print_value(
        f"\nrisk:       [{verdict_colour}][bold]{level.upper()}[/bold][/{verdict_colour}]"
    )

    if level in ("high", "invalid"):
        output.warn(
            "/send to this address would be REFUSED by the server "
            "(RISK_BLOCK_LEVEL=high default)."
        )
    elif level == "medium":
        output.info(
            "/send proceeds at default RISK_BLOCK_LEVEL=high; set "
            "RISK_BLOCK_LEVEL=medium in .env to also block on this."
        )
