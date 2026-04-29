"""``skr-crypto logs`` — tail recent service logs."""
from __future__ import annotations

import os

import click

from skr_crypto.cli import config, output, service


@click.command("logs")
@click.option("-f", "--follow", is_flag=True, help="Follow new lines.")
@click.option("-n", "--lines", default=200, show_default=True,
              help="Number of recent lines to show.")
@click.option("--via", type=click.Choice(["systemd", "compose", "direct"]),
              help="Force a specific lifecycle regime.")
@click.option("--audit-only", is_flag=True,
              help="Tail only the durable audit log (raw JSON-per-line).")
@click.pass_context
def cmd(
    ctx: click.Context,
    follow: bool,
    lines: int,
    via: str | None,
    audit_only: bool,
) -> None:
    """Tail recent logs.

    Backend is detected from the regime — journalctl for systemd,
    ``docker compose logs`` for compose. ``--audit-only`` bypasses both
    and tails ``data/audit.log`` directly, useful for forensic review
    where you only care about the financial events.
    """
    install_dir = config.require_installed(ctx.obj.get("install_dir"))

    if audit_only:
        env = config.read_env_file(install_dir / ".env")
        paths = config.ServicePaths.from_install_dir(install_dir, env)
        if not paths.audit_log.exists():
            output.warn(f"audit log not found at {paths.audit_log} "
                        "(it's created lazily on the first /send)")
            return
        argv = ["tail", f"-n{lines}"]
        if follow:
            argv.append("-f")
        argv.append(str(paths.audit_log))
    else:
        ctx_svc = service.detect(install_dir, override=via)
        argv = service.logs_command(ctx_svc, follow=follow, lines=lines)
        argv = [a for a in argv if a]  # drop empty strings

    # Replace this process so Ctrl-C goes to tail/journalctl directly.
    output.info(f"$ {' '.join(argv)}")
    os.execvp(argv[0], argv)
