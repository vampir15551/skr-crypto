"""``skr-crypto status`` — one-screen overview."""
from __future__ import annotations

import contextlib
import time

import click

from skr_crypto import config, output, service
from skr_crypto.api import APIClient
from skr_crypto.exceptions import (
    NotInstalledError,
    ServiceUnreachableError,
    SkrCryptoError,
)
from skr_crypto.installer import git_current_ref


@click.command("status")
@click.option("--json", "as_json", is_flag=True,
              help="Machine-readable output.")
@click.option("--via", type=click.Choice(["systemd", "compose", "direct"]),
              help="Force a specific lifecycle regime.")
@click.pass_context
def cmd(ctx: click.Context, as_json: bool, via: str | None) -> None:
    """One-screen status: installed? running? version? at-a-glance."""
    try:
        install_dir = config.require_installed(ctx.obj.get("install_dir"))
    except NotInstalledError:
        if as_json:
            output.print_json({"installed": False})
            return
        output.print_value("[red]not installed[/red]")
        output.info("Run `skr-crypto install` to bootstrap.")
        return

    payload: dict[str, object] = {
        "installed": True,
        "install_dir": str(install_dir),
    }

    # Git ref
    try:
        payload["ref"] = git_current_ref(install_dir)
    except Exception:
        payload["ref"] = "unknown"

    # Regime
    try:
        ctx_svc = service.detect(install_dir, override=via)
        payload["regime"] = ctx_svc.regime.value
    except Exception:
        payload["regime"] = "unknown"

    # API liveness + version
    try:
        client = APIClient.from_env_file(install_dir / ".env", timeout=3.0)
        live = client.health_live()
        payload["running"] = True
        payload["uptime_seconds"] = live.get("uptime_seconds", 0)
        try:
            v = client.version()
            payload["service_sha"] = v.get("git_sha", "unknown")
            payload["network"] = v.get("network", "unknown")
            payload["started_at"] = v.get("started_at")
        except SkrCryptoError as exc:
            payload["service_sha"] = f"error: {exc}"
    except ServiceUnreachableError:
        payload["running"] = False
    except SkrCryptoError as exc:
        payload["running"] = False
        payload["error"] = str(exc)

    if as_json:
        output.print_json(payload)
        return

    # Human-readable rendering
    table = output.make_table("Field", "Value", title="skr-crypto status")
    rows: list[tuple[str, str]] = [
        ("install dir", str(install_dir)),
        ("ref",         str(payload.get("ref", "?"))),
        ("regime",      str(payload.get("regime", "?"))),
    ]
    if payload.get("running"):
        rows.append(("running", "[green]yes[/green]"))
        uptime = int(payload.get("uptime_seconds", 0) or 0)
        rows.append(("uptime", _human_duration(uptime)))
        rows.append(("service git_sha", str(payload.get("service_sha", "?"))))
        rows.append(("network", str(payload.get("network", "?"))))
        if payload.get("started_at"):
            ts = payload["started_at"]
            with contextlib.suppress(ValueError, TypeError):
                rows.append((
                    "started",
                    time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(float(ts))),
                ))
    else:
        rows.append(("running", "[red]no[/red]"))
        if "error" in payload:
            rows.append(("error", str(payload["error"])))

    for k, v in rows:
        table.add_row(k, v)
    output.render_table(table)


def _human_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
