"""``skr-crypto start | stop | restart``."""
from __future__ import annotations

import click

from skr_crypto.cli import config, output, service
from skr_crypto.cli.exceptions import SkrCryptoError


def _do(ctx: click.Context, op_name: str, via: str | None) -> None:
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    ctx_svc = service.detect(install_dir, override=via)
    if ctx_svc.regime is service.Regime.DIRECT:
        raise SkrCryptoError(
            f"Cannot {op_name} a 'direct' install (no supervisor). "
            f"Either Ctrl-C / re-run ./run.sh manually, or set up systemd "
            f"(see deploy/payouts.service in the service repo)."
        )

    op = {"start": service.start, "stop": service.stop,
          "restart": service.restart}[op_name]
    output.info(f"{op_name.capitalize()}ing via {ctx_svc.regime.value}")
    result = op(ctx_svc)
    if result.returncode != 0:
        raise SkrCryptoError(
            f"{op_name} failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
    output.success(f"{op_name} ok")


@click.command("start")
@click.option("--via", type=click.Choice(["systemd", "compose", "direct"]))
@click.pass_context
def start_cmd(ctx: click.Context, via: str | None) -> None:
    """Start the installed service."""
    _do(ctx, "start", via)


@click.command("stop")
@click.option("--via", type=click.Choice(["systemd", "compose", "direct"]))
@click.pass_context
def stop_cmd(ctx: click.Context, via: str | None) -> None:
    """Stop the running service."""
    _do(ctx, "stop", via)


@click.command("restart")
@click.option("--via", type=click.Choice(["systemd", "compose", "direct"]))
@click.pass_context
def restart_cmd(ctx: click.Context, via: str | None) -> None:
    """Restart the service."""
    _do(ctx, "restart", via)
