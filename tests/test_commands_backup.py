"""``skr-crypto backup`` — snapshot data/."""
from __future__ import annotations

import tarfile

from skr_crypto.cli import cli


def test_backup_creates_archive(runner, isolated_install):
    # Seed some content
    (isolated_install / "data" / "audit.log").write_text("audit-line-1\n")
    (isolated_install / "data" / "idempotency.db").write_bytes(b"\x00sqlite\x00")

    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "backup",
    ])
    assert result.exit_code == 0, (result.stdout, result.stderr)

    archives = list((isolated_install / "backups").glob("data-*.tar.gz"))
    assert len(archives) == 1

    with tarfile.open(archives[0]) as tf:
        names = tf.getnames()
        assert "data/audit.log" in names
        assert "data/idempotency.db" in names


def test_backup_excludes_wal_shm(runner, isolated_install):
    """SQLite WAL/SHM are transient and not included in backups."""
    (isolated_install / "data" / "idempotency.db").write_bytes(b"db")
    (isolated_install / "data" / "idempotency.db-wal").write_bytes(b"wal")
    (isolated_install / "data" / "idempotency.db-shm").write_bytes(b"shm")

    runner.invoke(cli, ["--dir", str(isolated_install), "backup"])

    archive = next((isolated_install / "backups").glob("data-*.tar.gz"))
    with tarfile.open(archive) as tf:
        names = tf.getnames()
    assert "data/idempotency.db" in names
    assert "data/idempotency.db-wal" not in names
    assert "data/idempotency.db-shm" not in names


def test_backup_keep_prunes_old(runner, isolated_install):
    (isolated_install / "data" / "audit.log").write_text("ok\n")
    backups = isolated_install / "backups"
    backups.mkdir(exist_ok=True)
    # Create 5 fake old backups
    import os
    import time
    for i in range(5):
        f = backups / f"data-2020010{i}-000000.tar.gz"
        f.write_bytes(b"")
        # Older mtime each
        os.utime(f, (1577836800 + i, 1577836800 + i))
    time.sleep(0.01)

    runner.invoke(cli, [
        "--dir", str(isolated_install), "backup", "--keep", "3",
    ])
    remaining = list(backups.glob("data-*.tar.gz"))
    # Newest 3 retained — that includes the just-created one + 2 of the old.
    assert len(remaining) == 3


def test_backup_no_data_dir_errors(runner, tmp_path):
    """A bare install dir with no data/ should not silently emit an
    empty archive."""
    install = tmp_path / "install"
    install.mkdir()
    (install / "app").mkdir()
    (install / "app" / "__init__.py").write_text("")
    (install / ".env").write_text("AUTH_TOKEN=x\n")
    import os
    os.chmod(install / ".env", 0o600)

    result = runner.invoke(cli, [
        "--dir", str(install), "backup",
    ])
    # Generic SkrCryptoError → exit 1
    assert result.exit_code == 1
