"""``skr-crypto update`` — guidance on how to upgrade.

In the v1.0.0 single-package world, the upgrade is just::

    pip install -U <wheel-url-from-latest-github-release>

The CLI cannot run pip against itself reliably (it'd be installing
into the venv that's currently running), so this command is mostly a
documentation surface plus an optional safety helper that snapshots
``data/`` before you run pip yourself.
"""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.commands.backup import cmd as backup_cmd
from skr_crypto.cli.exceptions import SkrCryptoError
from skr_crypto.version import __version__

GITHUB_API_LATEST = (
    "https://api.github.com/repos/vampir15551/skr-crypto/releases/latest"
)


@click.command("update")
@click.option("--check-only", is_flag=True,
              help="Just print the latest available version, don't snapshot.")
@click.option("--no-backup", is_flag=True,
              help="Don't snapshot data/ before printing the upgrade command.")
@click.pass_context
def cmd(ctx: click.Context, check_only: bool, no_backup: bool) -> None:
    """Show the latest tagged release and how to upgrade to it.

    The actual ``pip install -U`` runs in your shell, not here — the
    CLI can't safely upgrade the venv it's executing in.
    """
    output.info(f"Current CLI: [bold]v{__version__}[/bold]")

    latest = _fetch_latest_release()
    if latest is None:
        output.warn("Could not reach GitHub API to check for updates.")
        output.info("Visit https://github.com/vampir15551/skr-crypto/releases manually.")
        return

    tag = latest.get("tag_name", "")
    if not tag:
        raise SkrCryptoError("GitHub returned a release with no tag_name")

    output.info(f"Latest release: [bold]{tag}[/bold]")

    if tag == f"v{__version__}":
        output.success("Already on the latest release.")
        return

    # Find the wheel asset URL.
    wheel_url = None
    for asset in latest.get("assets", []):
        if asset.get("name", "").endswith(".whl"):
            wheel_url = asset.get("browser_download_url")
            break
    if not wheel_url:
        output.warn(
            f"{tag} has no .whl asset attached — upgrade may not be ready."
        )
        return

    if check_only:
        output.print_value(wheel_url)
        return

    if not no_backup:
        try:
            install_dir = config.require_installed(ctx.obj.get("install_dir"))
            (install_dir / "data").mkdir(exist_ok=True)
            output.info("Snapshotting data/ before upgrade …")
            ctx.invoke(backup_cmd)
        except Exception as exc:
            output.warn(f"Snapshot failed: {exc} — continuing anyway.")

    output.info("")
    output.info("[bold]Run this in your shell to upgrade:[/bold]")
    output.print_value(f"  pip install -U {wheel_url}")
    output.info("")
    output.info("Then [italic]skr-crypto restart[/italic] to reload the service.")


def _fetch_latest_release() -> dict | None:
    """Best-effort GET of the latest release. None on any failure."""
    try:
        req = Request(
            GITHUB_API_LATEST,
            headers={"Accept": "application/vnd.github+json"},
        )
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None
