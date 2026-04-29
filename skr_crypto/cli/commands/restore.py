"""``skr-crypto restore <archive>`` — restore data/ from a backup."""
from __future__ import annotations

import shutil
import tarfile
import time
from pathlib import Path

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.exceptions import SkrCryptoError


@click.command("restore")
@click.argument("archive", type=click.Path(exists=True, dir_okay=False))
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation.")
@click.pass_context
def cmd(ctx: click.Context, archive: str, yes: bool) -> None:
    """Restore data/ from a previously taken backup.

    The current data/ is moved aside to ``data.pre-restore-<ts>/`` so
    a botched restore is recoverable. The service should be stopped
    first — restore() will warn but not stop you, since some
    failure-modes are diagnosable only with the service down.
    """
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    data = install_dir / "data"

    archive_path = Path(archive)
    output.info(f"restoring from {archive_path}")
    output.warn(f"current {data} will be moved to {data}.pre-restore-<ts>/")
    if not yes and not output.confirm("Proceed?", default=False):
        output.warn("aborted")
        ctx.exit(1)

    if data.exists():
        backup_aside = data.with_name(f"data.pre-restore-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.move(str(data), str(backup_aside))
        output.info(f"moved current data → {backup_aside}")

    install_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path, "r:gz") as tf:
            # Belt + braces: `filter='data'` (Python 3.12+) drops member
            # types we never produce in our own backups (devices, links,
             # set-uid bits) and rewrites paths to stay inside the
            # destination — defends against malicious archives even if
            # the file came from somewhere unexpected. Plus we still
            # walk and reject obvious traversals up front for a clearer
            # error message than tar's.
            for member in tf.getmembers():
                target = (install_dir / member.name).resolve()
                if not str(target).startswith(str(install_dir.resolve())):
                    raise SkrCryptoError(
                        f"refusing to extract suspect path: {member.name}"
                    )
            tf.extractall(install_dir, filter="data")
    except tarfile.TarError as exc:
        raise SkrCryptoError(f"could not read archive: {exc}") from exc

    output.success(f"restored {data}. Restart the service when ready.")
