"""``skr-crypto balance`` — current treasury TRX/USDT + on-chain resources."""
from __future__ import annotations

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.api import APIClient


@click.command("balance")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def cmd(ctx: click.Context, as_json: bool) -> None:
    """Show treasury TRX, USDT, and resource summary.

    Hits the service's ``/api/v1/balance`` endpoint, which itself
    queries TronGrid — expect ~1-3 seconds end-to-end. With a flaky
    node it can take longer; the request times out at 30s.
    """
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    client = APIClient.from_env_file(install_dir / ".env")
    payload = client.balance()

    if as_json:
        output.print_json(payload)
        return

    table = output.make_table("Field", "Value", title="Treasury balance")
    rows = [
        ("address",                   payload.get("address", "?")),
        ("TRX",                       str(payload.get("trx", "?"))),
        ("USDT",                      str(payload.get("usdt", "?"))),
        ("energy available",          str(payload.get("energy_available", 0))),
        ("bandwidth (free)",          str(payload.get("bandwidth_free_available", 0))),
        ("bandwidth (staked)",        str(payload.get("bandwidth_paid_available", 0))),
        ("TRON power staked",         str(payload.get("tron_power_staked", 0))),
    ]
    for k, v in rows:
        table.add_row(k, v)
    output.render_table(table)
