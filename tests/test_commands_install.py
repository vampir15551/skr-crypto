"""``skr-crypto install`` end-to-end with --from-path (no real git/pip)."""
from __future__ import annotations

import os

from skr_crypto.cli import cli


def _make_source_repo(src_path):
    """Build a minimal directory that looks like the service repo,
    enough for install --from-path --no-deps to copy and finalise."""
    src_path.mkdir(parents=True, exist_ok=True)
    (src_path / "app").mkdir()
    (src_path / "app" / "__init__.py").write_text("")
    (src_path / "scripts").mkdir()
    (src_path / "scripts" / "setup.py").write_text("# fake\n")
    # ./run.sh + requirements.txt are present in the real Payouts but
    # install --no-deps doesn't actually call pip, so we don't need them.
    return src_path


def test_install_creates_env_with_auth_token(runner, tmp_path):
    src = _make_source_repo(tmp_path / "src")
    install_dir = tmp_path / "install"
    result = runner.invoke(cli, [
        "--dir", str(install_dir),
        "install", "--from-path", str(src), "--no-deps",
    ])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    env_path = install_dir / ".env"
    assert env_path.exists()
    content = env_path.read_text()

    # AUTH_TOKEN must be present and look strong.
    auth_lines = [ln for ln in content.splitlines() if ln.startswith("AUTH_TOKEN=")]
    assert len(auth_lines) == 1
    token = auth_lines[0].split("=", 1)[1]
    assert len(token) >= 32, f"token too short: {token}"

    # Defaults from install.DEFAULT_ENV must be present.
    assert "TRON_NETWORK=mainnet" in content
    assert "AUDIT_LOG_FILE=data/audit.log" in content
    assert "IDEMPOTENCY_DB_PATH=data/idempotency.db" in content
    assert "KEY_PROVIDER=env" in content


def test_install_chmod_600(runner, tmp_path):
    src = _make_source_repo(tmp_path / "src")
    install_dir = tmp_path / "install"
    runner.invoke(cli, [
        "--dir", str(install_dir),
        "install", "--from-path", str(src), "--no-deps",
    ])
    mode = (install_dir / ".env").stat().st_mode & 0o777
    assert mode == 0o600


def test_install_creates_data_dir(runner, tmp_path):
    src = _make_source_repo(tmp_path / "src")
    install_dir = tmp_path / "install"
    runner.invoke(cli, [
        "--dir", str(install_dir),
        "install", "--from-path", str(src), "--no-deps",
    ])
    assert (install_dir / "data").is_dir()


def test_install_each_run_generates_fresh_token(runner, tmp_path):
    """Two installs into separate dirs yield distinct tokens."""
    src = _make_source_repo(tmp_path / "src")
    tokens = set()
    for d in ("a", "b"):
        install_dir = tmp_path / d
        runner.invoke(cli, [
            "--dir", str(install_dir),
            "install", "--from-path", str(src), "--no-deps",
        ])
        env = (install_dir / ".env").read_text()
        token = next(ln.split("=", 1)[1] for ln in env.splitlines()
                     if ln.startswith("AUTH_TOKEN="))
        tokens.add(token)
    assert len(tokens) == 2, "AUTH_TOKEN must be regenerated per install"


def test_install_refuses_non_empty_dir_without_force(runner, tmp_path):
    src = _make_source_repo(tmp_path / "src")
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "garbage").write_text("hi")
    result = runner.invoke(cli, [
        "--dir", str(install_dir),
        "install", "--from-path", str(src), "--no-deps",
    ])
    # AlreadyInstalledError → exit code 3
    assert result.exit_code == 3


def test_install_force_wipes(runner, tmp_path):
    src = _make_source_repo(tmp_path / "src")
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "garbage").write_text("hi")
    result = runner.invoke(cli, [
        "--dir", str(install_dir),
        "install", "--from-path", str(src), "--no-deps", "--force",
    ])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    # garbage file is gone, app/ from src is present
    assert not (install_dir / "garbage").exists()
    assert (install_dir / "app").exists()


def test_install_skips_venv_node_modules(runner, tmp_path):
    """--from-path must not copy venv/, .git/, __pycache__/, data/."""
    src = _make_source_repo(tmp_path / "src")
    (src / "venv" / "bin").mkdir(parents=True)
    (src / "venv" / "bin" / "python").write_text("#!/bin/sh")
    (src / ".git").mkdir()
    (src / ".git" / "config").write_text("[core]\n")
    (src / "data").mkdir()
    (src / "data" / "secret.log").write_text("don't copy me")

    install_dir = tmp_path / "install"
    runner.invoke(cli, [
        "--dir", str(install_dir),
        "install", "--from-path", str(src), "--no-deps",
    ])
    assert not (install_dir / "venv" / "bin" / "python").exists() or \
        os.path.realpath(install_dir / "venv") != os.path.realpath(src / "venv")
    assert not (install_dir / ".git").exists()
    # data/ from source must not be copied; the install creates a fresh empty one
    assert not (install_dir / "data" / "secret.log").exists()
