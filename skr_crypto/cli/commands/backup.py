"""``skr-crypto backup`` — snapshot data/."""
from __future__ import annotations

import tarfile
import time
from pathlib import Path

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.exceptions import SkrCryptoError


@click.command("backup")
@click.option("--out", "out_dir", default=None,
              type=click.Path(file_okay=False),
              help="Where to write the archive. Default: <install>/backups/")
@click.option("--keep", default=14, show_default=True,
              help="Retain at most this many recent backups.")
@click.pass_context
def cmd(ctx: click.Context, out_dir: str | None, keep: int) -> None:
    """Snapshot the service's data/ dir into a timestamped .tar.gz.

    Safe to run while the service is running — audit log is append+fsync,
    SQLite is in WAL mode so reads are non-blocking. Transient SQLite
    WAL/SHM files are excluded; reopening the .db rebuilds them.
    """
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    data = install_dir / "data"
    if not data.exists():
        raise SkrCryptoError(f"no data dir at {data} — nothing to back up")

    backups = Path(out_dir) if out_dir else install_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    out = backups / f"data-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"

    with tarfile.open(out, "w:gz") as tf:
        for child in data.iterdir():
            if child.suffix in (".db-wal", ".db-shm"):
                continue
            tf.add(child, arcname=f"data/{child.name}")

    size = out.stat().st_size
    output.success(f"wrote {out}  ({_human_size(size)})")

    # Prune old backups by mtime, newest first.
    archives = sorted(
        backups.glob("data-*.tar.gz"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in archives[keep:]:
        stale.unlink()
        output.info(f"pruned {stale.name}")


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f}TB"
