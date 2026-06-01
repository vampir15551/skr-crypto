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


def test_install_interactive_numbered_prompts(runner, tmp_path):
    """Numbered prompts. Order in install._collect_settings:

      [1] key provider  → numbered (1=env, 2=file, 3=1password, 4=keychain)
      [1.5] (file) key file path
      [2] network       → numbered (1=mainnet, 2=shasta, 3=nile)
      [3] trongrid api key (free text, hidden)
      [4] bind host (free text)
      [4] bind port (free text)
      [5] tunnel         → numbered (1=yes, 2=no)
      [6] gen key        → numbered (1=yes, 2=no)
      [7] advanced?      → numbered (1=yes, 2=no)
    """
    install_dir = tmp_path / "install"
    answers = "\n".join([
        "2",                          # key provider = file
        "/tmp/treasury.key",          # key file path
        "2",                          # network = shasta
        "TR-API",                     # trongrid api key
        "0.0.0.0",                    # bind host
        "9001",                       # bind port
        "1",                          # tunnel = yes
        "2",                          # gen-key = no
        "2",                          # advanced = no
    ]) + "\n"

    result = runner.invoke(
        cli, ["--dir", str(install_dir), "install", "--interactive"],
        input=answers,
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    text = (install_dir / ".env").read_text()
    assert "KEY_PROVIDER=file" in text
    assert "PRIVATE_KEY_FILE=/tmp/treasury.key" in text
    assert "TRON_NETWORK=shasta" in text
    assert "TRONGRID_API_KEY=TR-API" in text
    assert "SERVER_HOST=0.0.0.0" in text
    assert "SERVER_PORT=9001" in text
    assert "TUNNEL_ENABLED=1" in text


def test_install_interactive_typing_value_name_works(runner, tmp_path):
    """Numbered prompt also accepts the value name directly (`env`,
    `mainnet`) for operators who already know the choices."""
    install_dir = tmp_path / "install"
    answers = "\n".join([
        "env",        # key provider — by name
        "mainnet",    # network — by name
        "",           # trongrid (default empty)
        "",           # bind host (default 127.0.0.1)
        "",           # bind port (default 8000)
        "no",         # tunnel — by name
        "no",         # gen-key — by name
        "no",         # advanced — by name
    ]) + "\n"
    result = runner.invoke(
        cli, ["--dir", str(install_dir), "install", "--interactive"],
        input=answers,
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    text = (install_dir / ".env").read_text()
    assert "KEY_PROVIDER=env" in text
    assert "TRON_NETWORK=mainnet" in text


def test_install_interactive_1password_prompts_for_vault_item_field(runner, tmp_path):
    install_dir = tmp_path / "install"
    answers = "\n".join([
        "3",              # 1password
        "MyVault",        # OP_VAULT
        "MyItem",         # OP_ITEM
        "secret",         # OP_FIELD
        "1",              # network = mainnet
        "",               # trongrid empty
        "",               # bind host
        "",               # bind port
        "2",              # tunnel = no
        "2",              # gen-key = no
        "2",              # advanced = no
    ]) + "\n"
    result = runner.invoke(
        cli, ["--dir", str(install_dir), "install", "--interactive"],
        input=answers,
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    text = (install_dir / ".env").read_text()
    assert "KEY_PROVIDER=1password" in text
    assert "OP_VAULT=MyVault" in text
    assert "OP_ITEM=MyItem" in text
    assert "OP_FIELD=secret" in text


def test_install_interactive_keychain_prompts_for_service_account(runner, tmp_path):
    install_dir = tmp_path / "install"
    answers = "\n".join([
        "4",              # keychain
        "my-service",     # KEYCHAIN_SERVICE
        "my-account",     # KEYCHAIN_ACCOUNT
        "1",              # network mainnet
        "",               # trongrid
        "",               # bind host
        "",               # bind port
        "2",              # tunnel no
        "2",              # gen-key no
        "2",              # advanced no
    ]) + "\n"
    result = runner.invoke(
        cli, ["--dir", str(install_dir), "install", "--interactive"],
        input=answers,
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    text = (install_dir / ".env").read_text()
    assert "KEY_PROVIDER=keychain" in text
    assert "KEYCHAIN_SERVICE=my-service" in text
    assert "KEYCHAIN_ACCOUNT=my-account" in text


def test_install_interactive_invalid_number_reprompts(runner, tmp_path):
    """Out-of-range or non-digit input must reprompt, not crash."""
    install_dir = tmp_path / "install"
    answers = "\n".join([
        "9",              # invalid (only 1..4 for key provider)
        "bogus",          # invalid (not a digit, not a name)
        "1",              # finally: env
        "1",              # network mainnet
        "",               # trongrid
        "",               # bind host
        "",               # bind port
        "2",              # tunnel
        "2",              # gen-key
        "2",              # advanced
    ]) + "\n"
    result = runner.invoke(
        cli, ["--dir", str(install_dir), "install", "--interactive"],
        input=answers,
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "KEY_PROVIDER=env" in (install_dir / ".env").read_text()


def test_install_interactive_explicit_flag_skips_prompt(runner, tmp_path):
    """A flag value bypasses its prompt entirely. Test by passing
    --network and verifying we only need to feed the remaining
    answers."""
    install_dir = tmp_path / "install"
    answers = "\n".join([
        "1",              # key provider env
        # no network prompt — passed via flag
        "",               # trongrid empty
        "",               # bind host default
        "",               # bind port default
        "2",              # tunnel no
        "2",              # gen-key no
        "2",              # advanced no
    ]) + "\n"
    result = runner.invoke(
        cli,
        ["--dir", str(install_dir), "install", "--interactive",
         "--network", "nile"],
        input=answers,
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "TRON_NETWORK=nile" in (install_dir / ".env").read_text()


def test_install_advanced_flag_prompts_advanced(runner, tmp_path):
    install_dir = tmp_path / "install"
    answers = "\n".join([
        "1",              # env
        "1",              # mainnet
        "",               # trongrid
        "",               # bind host
        "",               # bind port
        "2",              # tunnel no
        "2",              # gen-key no
        "2",              # configure Telegram alerts? — no (1.9.0+ step)
        # advanced is forced via flag — but the wizard still shows the
        # toggle question? In our flow, --advanced flag means "show the
        # prompts" and skips the yes/no. Let's feed the per-knob answers.
        "100",            # MIN_TRX_RESERVE
        "30",             # MAX_ENERGY_BURN_TRX
        "900",            # SHUTDOWN_TIMEOUT
        "60",             # RATE_LIMIT_MAX
        "30",             # RATE_LIMIT_WINDOW
    ]) + "\n"
    result = runner.invoke(
        cli, ["--dir", str(install_dir), "install",
              "--interactive", "--advanced"],
        input=answers,
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    text = (install_dir / ".env").read_text()
    assert "MIN_TRX_RESERVE=100" in text
    assert "MAX_ENERGY_BURN_TRX=30" in text
    assert "SHUTDOWN_TIMEOUT=900" in text
    assert "RATE_LIMIT_MAX=60" in text
    assert "RATE_LIMIT_WINDOW=30" in text


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
