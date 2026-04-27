"""``skr-crypto version`` — CLI version + service version (if installed)."""
from __future__ import annotations

import click

from skr_crypto import config, output
from skr_crypto.api import APIClient
from skr_crypto.exceptions import (
    NotInstalledError,
    ServiceUnreachableError,
    SkrCryptoError,
)
from skr_crypto.version import __version__


@click.command("version")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def cmd(ctx: click.Context, as_json: bool) -> None:
    """Print CLI and service versions.

    The CLI version is always known (built into the package). The
    service version is fetched from ``/api/v1/version`` if the service
    is running; otherwise we just say "not running".
    """
    payload: dict[str, object] = {"cli": __version__}
    try:
        install_dir = config.require_installed(ctx.obj.get("install_dir"))
    except NotInstalledError:
        payload["service"] = "not installed"
    else:
        try:
            client = APIClient.from_env_file(install_dir / ".env")
            v = client.version()
            payload["service"] = {
                "git_sha": v.get("git_sha", "unknown"),
                "started_at": v.get("started_at"),
                "network": v.get("network"),
            }
        except ServiceUnreachableError:
            payload["service"] = "not running"
        except SkrCryptoError as exc:
            payload["service"] = f"error: {exc}"

    if as_json:
        output.print_json(payload)
        return

    output.print_value(f"skr-crypto: [bold]{__version__}[/bold]")
    svc = payload["service"]
    if isinstance(svc, dict):
        output.print_value(
            f"service:    git_sha={svc['git_sha']}  network={svc['network']}"
        )
    else:
        output.print_value(f"service:    {svc}")
