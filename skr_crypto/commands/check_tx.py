"""``skr-crypto check <txid>`` — read-only on-chain status look-up.

Drives the service's TronClient through a tiny inline Python helper.
We don't add an HTTP endpoint just for this — that would mean keeping
two interfaces in sync. Instead the CLI runs a one-liner inside the
service's venv that prints JSON to stdout.
"""
from __future__ import annotations

import json
import subprocess

import click

from skr_crypto import config, output
from skr_crypto.exceptions import SkrCryptoError

_PROBE_SCRIPT = r"""
import json, sys
from app.tron_client import tron
tron.init()
txid = sys.argv[1]
try:
    info = tron.client.get_transaction_info(txid)
except Exception as exc:
    print(json.dumps({"error": str(exc), "type": type(exc).__name__}))
    sys.exit(0)
if not info:
    print(json.dumps({"status": "NOT_FOUND"}))
else:
    receipt = info.get("receipt") or {}
    out = {
        "status": receipt.get("result") or "SUCCESS",
        "block_timestamp": info.get("blockTimeStamp"),
        "energy_used": receipt.get("energy_usage_total", 0),
        "fee": info.get("fee", 0),
        "raw": info,
    }
    print(json.dumps(out, default=str))
"""


@click.command("check")
@click.argument("txid")
@click.option("--json", "as_json", is_flag=True, help="Print the raw response.")
@click.pass_context
def cmd(ctx: click.Context, txid: str, as_json: bool) -> None:
    """Look up a tx hash on-chain.

    Returns one of:

      SUCCESS       transfer landed and the contract call succeeded
      OUT_OF_ENERGY broadcast OK but on-chain reverted (not enough energy)
      REVERT        contract reverted (USDT rules / blacklist / paused)
      NOT_FOUND     no record on this RPC endpoint
      <other>       any other Tron receipt code

    This call goes via TronGrid using the service's API key, so it
    counts against your rate limit.
    """
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    py = install_dir / "venv" / "bin" / "python"
    if not py.exists():
        raise SkrCryptoError(
            f"venv python not found at {py} — service may be partially installed"
        )

    result = subprocess.run(
        [str(py), "-c", _PROBE_SCRIPT, txid],
        cwd=str(install_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise SkrCryptoError(
            f"probe failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()}"
        )

    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise SkrCryptoError(
            f"could not parse probe output: {result.stdout!r}"
        ) from exc

    if as_json:
        output.print_json(payload)
        return

    if "error" in payload:
        output.error(f"{payload['type']}: {payload['error']}")
        ctx.exit(6)

    table = output.make_table("Field", "Value", title=f"tx {txid}")
    table.add_row("status", _colour_status(payload.get("status", "?")))
    if "block_timestamp" in payload:
        table.add_row("block_timestamp", str(payload["block_timestamp"]))
    table.add_row("energy_used", str(payload.get("energy_used", 0)))
    table.add_row("fee (sun)", str(payload.get("fee", 0)))
    output.render_table(table)


def _colour_status(s: str) -> str:
    if s == "SUCCESS":
        return f"[green]{s}[/green]"
    if s in ("NOT_FOUND",):
        return f"[yellow]{s}[/yellow]"
    return f"[red]{s}[/red]"
