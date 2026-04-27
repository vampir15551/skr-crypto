"""``skr-crypto update`` — pull a new version + restart.

The flow is deliberately conservative:

  1. ``git fetch --tags`` against the install dir.
  2. Determine the target ref:
       - explicit ``--version vX.Y.Z`` if given,
       - otherwise the highest ``v*`` tag.
  3. If we're already at that ref → no-op, exit 0.
  4. Show the CHANGELOG diff between current ref and target ref.
  5. Confirm (skip with ``--yes``).
  6. Snapshot ``data/`` first (unless ``--no-backup``) — restoring on
     a bad upgrade is one command.
  7. ``git checkout`` + ``pip install``.
  8. Restart the service via the detected regime
     (systemd / compose / direct).
"""
from __future__ import annotations

from pathlib import Path

import click

from skr_crypto import config, output, service
from skr_crypto.exceptions import SkrCryptoError, VenvError
from skr_crypto.installer import (
    git_checkout,
    git_current_ref,
    git_diff_changelog,
    git_fetch_tags,
    git_latest_tag,
    pip_install,
)


@click.command("update")
@click.option("--version", "version", default=None,
              help="Specific version (tag or ref) to update to. Default: latest tag.")
@click.option("--yes", "-y", is_flag=True,
              help="Skip the confirmation prompt.")
@click.option("--no-backup", is_flag=True,
              help="Don't snapshot data/ before updating.")
@click.option("--no-restart", is_flag=True,
              help="Apply the update but don't restart the service.")
@click.option("--via", type=click.Choice(["systemd", "compose", "direct"]),
              help="Force a specific lifecycle regime. Default: auto-detect.")
@click.pass_context
def cmd(
    ctx: click.Context,
    version: str | None,
    yes: bool,
    no_backup: bool,
    no_restart: bool,
    via: str | None,
) -> None:
    """Update the installed service to a newer version."""
    install_dir = config.require_installed(ctx.obj.get("install_dir"))

    # ── 1. Fetch + decide target ────────────────────────────────────────
    output.info("Fetching tags from origin")
    git_fetch_tags(install_dir)
    current = git_current_ref(install_dir)

    target = version or git_latest_tag(install_dir)
    if not target:
        raise SkrCryptoError(
            "No tags found in repo and no --version given. Either tag a "
            "release upstream or pass --version <ref>."
        )

    if current == target:
        output.success(f"Already at {current} — nothing to do.")
        return

    output.info(f"Current: [yellow]{current}[/yellow]  →  Target: [green]{target}[/green]")

    # ── 2. CHANGELOG diff ───────────────────────────────────────────────
    diff = git_diff_changelog(install_dir, current, target)
    if diff:
        output.info("[bold]CHANGELOG diff:[/bold]")
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                output.info(f"  [green]{line}[/green]")
            elif line.startswith("-") and not line.startswith("---"):
                output.info(f"  [red]{line}[/red]")
            else:
                output.info(f"  {line}")
    else:
        output.warn("No CHANGELOG.md diff available between these refs.")

    # ── 3. Confirm ──────────────────────────────────────────────────────
    if not yes and not output.confirm("Apply update?", default=True):
        output.warn("Aborted.")
        ctx.exit(1)

    # ── 4. Backup ───────────────────────────────────────────────────────
    if not no_backup:
        backup_path = _snapshot_data_dir(install_dir)
        output.success(f"snapshot saved: {backup_path}")
    else:
        output.warn("--no-backup: skipping data/ snapshot")

    # ── 5. Apply ────────────────────────────────────────────────────────
    output.info(f"Checking out {target}")
    git_checkout(install_dir, target)

    output.info("Reinstalling dependencies")
    try:
        pip_install(install_dir / "venv" / "bin" / "python", install_dir)
    except VenvError as exc:
        output.error(str(exc))
        output.warn(
            "Update is half-applied. Restore from snapshot if needed: "
            "skr-crypto restore <archive>"
        )
        raise

    output.success(f"Code at {target}")

    # ── 6. Restart ──────────────────────────────────────────────────────
    if no_restart:
        output.warn("--no-restart: leaving the service untouched. "
                    "Run `skr-crypto restart` when ready.")
        return
    ctx_svc = service.detect(install_dir, override=via)
    if ctx_svc.regime is service.Regime.DIRECT:
        output.warn(
            "Service runs in 'direct' regime (no supervision) — "
            "Ctrl-C the running ./run.sh and restart it manually."
        )
        return
    output.info(f"Restarting via {ctx_svc.regime.value}")
    result = service.restart(ctx_svc)
    if result.returncode != 0:
        raise SkrCryptoError(
            f"restart failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
    output.success("service restarted")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _snapshot_data_dir(install_dir: Path) -> Path:
    """Mirror what ``scripts/backup.sh`` does, but without depending on
    the script being present on disk (it might not be in older
    installs)."""
    import tarfile
    import time

    data = install_dir / "data"
    if not data.exists():
        # Nothing to back up — first install, audit not yet written.
        return data
    backups = install_dir / "backups"
    backups.mkdir(exist_ok=True)
    out = backups / f"data-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        for child in data.iterdir():
            # Skip transient SQLite WAL/SHM files — re-opening rebuilds them.
            if child.suffix in (".db-wal", ".db-shm"):
                continue
            tf.add(child, arcname=f"data/{child.name}")
    return out
