"""``skr-crypto reconcile`` — run startup_check on demand.

The service does this automatically at boot. ``reconcile`` lets the
operator trigger it without restarting — useful after a power cut /
reconnect, or just to spot-check a suspicious settle without taking
the service down.
"""
from __future__ import annotations

import subprocess

import click

from skr_crypto import config, output
from skr_crypto.exceptions import SkrCryptoError


@click.command("reconcile")
@click.pass_context
def cmd(ctx: click.Context) -> None:
    """Run the service's startup_check.run_startup_check() once."""
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    py = install_dir / "venv" / "bin" / "python"
    if not py.exists():
        raise SkrCryptoError(
            f"venv python not found at {py} — service may be partially installed"
        )

    output.info("Running reconciliation (this may take ~5-30s depending on tx count)")
    result = subprocess.run(
        [
            str(py), "-c",
            "from app.tron_client import tron;"
            "tron.init();"
            "from app.startup_check import run_startup_check;"
            "run_startup_check()",
        ],
        cwd=str(install_dir),
        # Stream output live so the operator sees [STARTUP_CHECK] lines
        # as they happen rather than waiting for the whole run.
        check=False,
    )
    if result.returncode != 0:
        raise SkrCryptoError(
            f"reconcile exited with status {result.returncode}"
        )
    output.success("done")
