"""``skr-crypto install`` v1.1+ — interactive wizard + flags + tunnel toggle.

We never run the actual interactive prompts in tests — every test
either passes ``--yes`` (so install is non-interactive) or feeds the
prompt with ``runner.invoke(input=...)``. The fixture filesystem in
``isolated_install`` is reused for the "already installed" branch.
"""
from __future__ import annotations

import os

from skr_crypto.cli.main import cli


def test_install_yes_creates_env_and_data(runner, tmp_path):
    """`--yes` is the supported automation entrypoint. It must produce
    a chmod-600 .env, a data/ dir, and an AUTH_TOKEN."""
    install_dir = tmp_path / "install"
    result = runner.invoke(cli, [
        "--dir", str(install_dir), "install", "--yes",
    ])
    assert result.exit_code == 0, (result.stdout, result.stderr)

    env_path = install_dir / ".env"
    assert env_path.exists()
    assert (env_path.stat().st_mode & 0o777) == 0o600
    assert (install_dir / "data").is_dir()

    text = env_path.read_text()
    assert "AUTH_TOKEN=" in text
    token_line = next(ln for ln in text.splitlines()
                      if ln.startswith("AUTH_TOKEN="))
    assert len(token_line.split("=", 1)[1]) >= 32


def test_install_yes_writes_safe_defaults(runner, tmp_path):
    install_dir = tmp_path / "install"
    runner.invoke(cli, ["--dir", str(install_dir), "install", "--yes"])
    text = (install_dir / ".env").read_text()
    assert "TRON_NETWORK=mainnet" in text
    assert "KEY_PROVIDER=env" in text
    assert "AUDIT_LOG_FILE=data/audit.log" in text
    assert "IDEMPOTENCY_DB_PATH=data/idempotency.db" in text
    assert "TUNNEL_ENABLED=0" in text  # tunnel off by default


def test_install_with_tunnel_flag(runner, tmp_path):
    install_dir = tmp_path / "install"
    result = runner.invoke(cli, [
        "--dir", str(install_dir), "install", "--yes", "--tunnel",
    ])
    assert result.exit_code == 0
    assert "TUNNEL_ENABLED=1" in (install_dir / ".env").read_text()


def test_install_with_no_tunnel_flag(runner, tmp_path):
    install_dir = tmp_path / "install"
    result = runner.invoke(cli, [
        "--dir", str(install_dir), "install", "--yes", "--no-tunnel",
    ])
    assert result.exit_code == 0
    assert "TUNNEL_ENABLED=0" in (install_dir / ".env").read_text()


def test_install_explicit_flags_override_defaults(runner, tmp_path):
    install_dir = tmp_path / "install"
    result = runner.invoke(cli, [
        "--dir", str(install_dir), "install", "--yes",
        "--key-provider", "file",
        "--network", "shasta",
        "--trongrid-api-key", "TROOOOOO",
        "--bind-host", "0.0.0.0",
        "--bind-port", "9000",
    ])
    assert result.exit_code == 0
    text = (install_dir / ".env").read_text()
    assert "KEY_PROVIDER=file" in text
    assert "TRON_NETWORK=shasta" in text
    assert "TRONGRID_API_KEY=TROOOOOO" in text
    assert "SERVER_HOST=0.0.0.0" in text
    assert "SERVER_PORT=9000" in text


def test_install_invalid_key_provider_rejected(runner, tmp_path):
    install_dir = tmp_path / "install"
    result = runner.invoke(cli, [
        "--dir", str(install_dir), "install", "--yes",
        "--key-provider", "bogus",
    ])
    # click rejects invalid choice with usage error; exit != 0.
    assert result.exit_code != 0
    assert "bogus" in (result.stdout + result.stderr).lower() or \
           "invalid value" in (result.stdout + result.stderr).lower()


def test_install_refuses_existing_without_force(runner, tmp_path):
    install_dir = tmp_path / "install"
    runner.invoke(cli, ["--dir", str(install_dir), "install", "--yes"])

    result = runner.invoke(cli, [
        "--dir", str(install_dir), "install", "--yes",
    ])
    # AlreadyInstalledError → exit 3
    assert result.exit_code == 3


def test_install_force_wipes_and_regenerates_token(runner, tmp_path):
    install_dir = tmp_path / "install"
    runner.invoke(cli, ["--dir", str(install_dir), "install", "--yes"])
    first = (install_dir / ".env").read_text()
    first_token = next(ln.split("=", 1)[1] for ln in first.splitlines()
                       if ln.startswith("AUTH_TOKEN="))

    result = runner.invoke(cli, [
        "--dir", str(install_dir), "install", "--yes", "--force",
    ])
    assert result.exit_code == 0
    second = (install_dir / ".env").read_text()
    second_token = next(ln.split("=", 1)[1] for ln in second.splitlines()
                        if ln.startswith("AUTH_TOKEN="))
    assert first_token != second_token, "force-install must regenerate AUTH_TOKEN"


def test_install_quiet_suppresses_progress(runner, tmp_path):
    install_dir = tmp_path / "install"
    result = runner.invoke(cli, [
        "--quiet", "--dir", str(install_dir), "install", "--yes",
    ])
    assert result.exit_code == 0
    # Quiet must drop the progress / success lines on stderr.
    assert "Installing into" not in result.stderr
    assert "wrote" not in result.stderr
    # The .env still gets written.
    assert (install_dir / ".env").exists()


def test_install_interactive_via_prompts(runner, tmp_path):
    """Non-TTY default is non-interactive; --interactive forces the
    wizard. Feed prompts with `input=...`. Order matches install.py:
    key_provider → network → trongrid → bind_host → bind_port → tunnel
    → 'generate fresh key now?'."""
    install_dir = tmp_path / "install"
    answers = "\n".join([
        "file",       # key provider
        "shasta",     # network
        "TR-API",     # trongrid api key
        "0.0.0.0",    # bind host
        "9001",       # bind port
        "y",          # enable tunnel
        "n",          # generate fresh key — no
    ]) + "\n"

    result = runner.invoke(
        cli, ["--dir", str(install_dir), "install", "--interactive"],
        input=answers,
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    text = (install_dir / ".env").read_text()
    assert "KEY_PROVIDER=file" in text
    assert "TRON_NETWORK=shasta" in text
    assert "TRONGRID_API_KEY=TR-API" in text
    assert "SERVER_HOST=0.0.0.0" in text
    assert "SERVER_PORT=9001" in text
    assert "TUNNEL_ENABLED=1" in text


def test_install_explicit_flag_skips_prompt_in_interactive(runner, tmp_path):
    """If --network is passed, the wizard does NOT prompt for it.
    We test by feeding only the OTHER answers and verifying no prompt
    starvation happens."""
    install_dir = tmp_path / "install"
    answers = "\n".join([
        # network is provided via flag; no prompt for it
        "env",        # key provider
        "",           # trongrid api key (empty)
        "127.0.0.1",  # bind host
        "8000",       # bind port
        "n",          # tunnel
        "n",          # gen-key
    ]) + "\n"

    result = runner.invoke(
        cli,
        ["--dir", str(install_dir), "install", "--interactive",
         "--network", "nile"],
        input=answers,
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "TRON_NETWORK=nile" in (install_dir / ".env").read_text()


def test_install_help_shows_new_flags(runner):
    result = runner.invoke(cli, ["install", "--help"])
    assert result.exit_code == 0
    for flag in ("--yes", "--interactive", "--force", "--key-provider",
                 "--network", "--trongrid-api-key", "--bind-host",
                 "--bind-port", "--tunnel", "--gen-key"):
        assert flag in result.output, f"missing {flag} from --help"


def test_install_chmod_when_umask_unusual(runner, tmp_path):
    """Even with an unusual process umask, .env must come out 0600."""
    old = os.umask(0o022)
    try:
        install_dir = tmp_path / "install"
        runner.invoke(cli, ["--dir", str(install_dir), "install", "--yes"])
        assert (install_dir / ".env").stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)
