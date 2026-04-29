"""``skr-crypto install`` — bootstrap a service install dir.

This used to clone the service code from a separate git repo. Since
v1.0.0 the service ships in the same package as the CLI (one
``pip install skr-crypto[server]`` gives you both), so there is nothing
to clone. ``install`` now just lays down the runtime state directory:

  1. ``$SKR_CRYPTO_HOME`` / ``--dir`` (default ``~/.skr-crypto``)
  2. ``.env`` with a generated high-entropy ``AUTH_TOKEN`` and safe
     defaults that **do not** yet include a private key — the operator
     picks a key provider afterwards.
  3. ``data/`` — where the audit log + idempotency DB land.

Re-runs against an existing install are blocked unless ``--force``,
which wipes the directory.
"""
from __future__ import annotations

import secrets

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.exceptions import AlreadyInstalledError

# Defaults written into .env on a fresh install. Editable later via
# `skr-crypto config edit`.
DEFAULT_ENV: dict[str, str] = {
    "TRON_NETWORK": "mainnet",
    "TRONGRID_API_KEY": "",  # operator must fill in
    "USDT_CONTRACT": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
    "KEY_PROVIDER": "env",   # safest default: secret comes from env, not disk
    "MIN_TRX_RESERVE": "50",
    "MAX_ENERGY_BURN_TRX": "20",
    "AUDIT_LOG_FILE": "data/audit.log",
    "IDEMPOTENCY_DB_PATH": "data/idempotency.db",
    "SERVER_HOST": "127.0.0.1",
    "SERVER_PORT": "8000",
    "SHUTDOWN_TIMEOUT": "600",
    "RATE_LIMIT_MAX": "30",
    "RATE_LIMIT_WINDOW": "60",
}


@click.command("install")
@click.option("--force", is_flag=True,
              help="Wipe an existing install dir before installing.")
@click.pass_context
def cmd(ctx: click.Context, force: bool) -> None:
    """Initialise the runtime state directory for the service.

    Service code is already part of this package — there is nothing
    to clone. This command writes ``.env`` + creates ``data/``.
    """
    install_dir = config.install_dir(ctx.obj.get("install_dir"))

    already_installed = (
        install_dir.exists()
        and (install_dir / ".env").exists()
    )
    if already_installed and not force:
        raise AlreadyInstalledError(
            f"Already installed at {install_dir}. Use --force to wipe and "
            f"reinstall, or --dir to install elsewhere."
        )
    if already_installed:
        output.warn(f"--force: removing {install_dir}")
        _rmtree(install_dir)

    install_dir.mkdir(parents=True, exist_ok=True)
    (install_dir / "data").mkdir(exist_ok=True)
    output.info(f"Installing into [bold]{install_dir}[/bold]")

    auth_token = secrets.token_urlsafe(32)
    env_values = dict(DEFAULT_ENV)
    env_values["AUTH_TOKEN"] = auth_token
    env_path = install_dir / ".env"
    config.write_env_file(env_path, env_values)
    output.success(f"wrote {env_path} (mode 0600)")
    output.success(f"created {install_dir / 'data'}")
    output.success("generated fresh AUTH_TOKEN")

    output.info("")
    output.info("[bold]Next steps:[/bold]")
    output.info("  1. Set the private key. With KEY_PROVIDER=env (default), export it:")
    output.info("       export PRIVATE_KEY_HEX='<32-byte-hex>'")
    output.info("     Or generate a fresh one: [italic]skr-crypto keygen[/italic]")
    output.info("     Or switch backend:       [italic]skr-crypto config edit[/italic]")
    output.info("  2. Set TRONGRID_API_KEY in .env (highly recommended).")
    output.info("  3. Start: [italic]skr-crypto start[/italic]")
    output.info("  4. Verify: [italic]skr-crypto status[/italic]")
    output.info("")
    output.info(f"AUTH_TOKEN (also in .env): [yellow]{auth_token}[/yellow]")
    output.info("Save this — your API clients need it as the X-API-Key header.")


def _rmtree(path) -> None:
    import shutil
    shutil.rmtree(path)
