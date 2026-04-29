"""``skr-crypto install`` — bootstrap a service install dir.

Interactive by default: walks the operator through the choices that
matter at first install (key provider, network, TronGrid API key,
bind, tunnel, optional fresh key generation). Every prompt has an
explicit flag so the same command can be run unattended in CI:

    skr-crypto install                                  # wizard
    skr-crypto install --yes                            # accept defaults
    skr-crypto install --key-provider file --network mainnet \
        --bind-host 0.0.0.0 --tunnel --yes              # explicit
    skr-crypto install --interactive                    # force wizard

Service code is part of this Python package — the command writes
``.env`` + creates ``data/`` + (optionally) generates a fresh TRON
private key. There is nothing to clone.
"""
from __future__ import annotations

import secrets
import shutil
from pathlib import Path

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.exceptions import AlreadyInstalledError

# Defaults for everything askable. Used both as click defaults and as
# wizard-prompt defaults; the source of truth is one place.
DEFAULTS = {
    "key_provider": "env",
    "network": "mainnet",
    "trongrid_api_key": "",
    "bind_host": "127.0.0.1",
    "bind_port": 8000,
    "tunnel": False,
    "audit_log": "data/audit.log",
    "idempotency_db": "data/idempotency.db",
    "shutdown_timeout": 600,
    "min_trx_reserve": "50",
    "max_energy_burn_trx": "20",
    "rate_limit_max": 30,
    "rate_limit_window": 60,
    "usdt_contract": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
}

KEY_PROVIDERS = ("env", "file", "1password", "keychain")
NETWORKS = ("mainnet", "shasta", "nile")


@click.command("install")
# Mode flags ----------------------------------------------------------
@click.option("-y", "--yes", is_flag=True,
              help="Non-interactive: accept defaults / explicit flags.")
@click.option("--interactive/--no-interactive", default=None,
              help="Force the wizard on/off. Default: wizard if STDIN is a TTY "
                   "and --yes was not given.")
@click.option("--force", is_flag=True,
              help="Wipe an existing install dir before installing.")
# Per-setting flags --------------------------------------------------
@click.option("--key-provider",
              type=click.Choice(KEY_PROVIDERS, case_sensitive=False),
              default=None,
              help=f"How the service loads the private key. "
                   f"Default: {DEFAULTS['key_provider']}.")
@click.option("--network",
              type=click.Choice(NETWORKS, case_sensitive=False),
              default=None,
              help=f"TRON network. Default: {DEFAULTS['network']}.")
@click.option("--trongrid-api-key", default=None,
              help="TronGrid API key (without it the free tier rate-limits hit fast).")
@click.option("--bind-host", default=None,
              help=f"Bind address. Default: {DEFAULTS['bind_host']}.")
@click.option("--bind-port", type=int, default=None,
              help=f"TCP port. Default: {DEFAULTS['bind_port']}.")
@click.option("--tunnel/--no-tunnel", default=None,
              help="Auto-start a Cloudflare quick-tunnel from run.sh on boot. "
                   "Requires `cloudflared` on PATH at run time.")
@click.option("--gen-key", is_flag=True,
              help="After writing .env, generate a fresh TRON private key.")
@click.pass_context
def cmd(
    ctx: click.Context,
    yes: bool,
    interactive: bool | None,
    force: bool,
    key_provider: str | None,
    network: str | None,
    trongrid_api_key: str | None,
    bind_host: str | None,
    bind_port: int | None,
    tunnel: bool | None,
    gen_key: bool,
) -> None:
    """Initialise the runtime state directory for the service.

    Service code is already part of this package — this command writes
    ``.env`` + creates ``data/``. With ``--gen-key`` it also runs
    ``skr-crypto keygen`` after install.
    """
    install_dir = config.install_dir(ctx.obj.get("install_dir"))

    # Mode: explicit flag wins, then --yes, then auto-detect TTY.
    import sys
    if interactive is None:
        interactive = (not yes) and sys.stdin.isatty()

    # ── 1. Refuse / wipe existing install ────────────────────────────────
    already_installed = (
        install_dir.exists() and (install_dir / ".env").exists()
    )
    if already_installed and not force:
        raise AlreadyInstalledError(
            f"Already installed at {install_dir}. Use --force to wipe and "
            f"reinstall, or --dir to install elsewhere."
        )
    if already_installed:
        if interactive and not output.confirm(
            f"--force will delete {install_dir} entirely. Continue?",
            default=False,
        ):
            output.warn("Aborted.")
            ctx.exit(1)
        output.warn(f"--force: removing {install_dir}")
        shutil.rmtree(install_dir)

    install_dir.mkdir(parents=True, exist_ok=True)
    (install_dir / "data").mkdir(exist_ok=True)
    output.info(f"Installing into [bold]{install_dir}[/bold]")
    output.info("")

    # ── 2. Resolve every setting (flag > prompt > default) ───────────────
    # Each call returns the final value; flags pre-empt prompts.
    key_provider = _resolve_choice(
        "Key provider",
        key_provider,
        KEY_PROVIDERS,
        DEFAULTS["key_provider"],
        interactive,
        explainer=(
            "  env       PRIVATE_KEY_HEX from process env (containers, CI)\n"
            "  file      chmod-600 file at PRIVATE_KEY_FILE (VPS / systemd)\n"
            "  1password 1Password CLI (`op`); operator-on-laptop\n"
            "  keychain  macOS Keychain (local dev)"
        ),
    )
    network = _resolve_choice(
        "TRON network",
        network,
        NETWORKS,
        DEFAULTS["network"],
        interactive,
    )
    trongrid_api_key = _resolve_string(
        "TronGrid API key (recommended, blank to skip)",
        trongrid_api_key,
        DEFAULTS["trongrid_api_key"],
        interactive,
        secret=True,
    )
    bind_host = _resolve_string(
        "Bind host",
        bind_host,
        DEFAULTS["bind_host"],
        interactive,
    )
    bind_port_str = _resolve_string(
        "Bind port",
        str(bind_port) if bind_port else None,
        str(DEFAULTS["bind_port"]),
        interactive,
    )
    try:
        bind_port_int = int(bind_port_str)
    except ValueError:
        output.warn(f"Bind port {bind_port_str!r} is not an integer; using default {DEFAULTS['bind_port']}.")
        bind_port_int = DEFAULTS["bind_port"]
    tunnel = _resolve_bool(
        "Auto-start Cloudflare quick-tunnel on `skr-crypto start`",
        tunnel,
        DEFAULTS["tunnel"],
        interactive,
        explainer=_tunnel_explainer(),
    )

    # ── 3. Write .env ────────────────────────────────────────────────────
    auth_token = secrets.token_urlsafe(32)
    env_values = {
        "AUTH_TOKEN": auth_token,
        "TRON_NETWORK": network,
        "TRONGRID_API_KEY": trongrid_api_key,
        "USDT_CONTRACT": DEFAULTS["usdt_contract"],
        "KEY_PROVIDER": key_provider,
        "MIN_TRX_RESERVE": DEFAULTS["min_trx_reserve"],
        "MAX_ENERGY_BURN_TRX": DEFAULTS["max_energy_burn_trx"],
        "AUDIT_LOG_FILE": DEFAULTS["audit_log"],
        "IDEMPOTENCY_DB_PATH": DEFAULTS["idempotency_db"],
        "SERVER_HOST": bind_host,
        "SERVER_PORT": str(bind_port_int),
        "SHUTDOWN_TIMEOUT": str(DEFAULTS["shutdown_timeout"]),
        "RATE_LIMIT_MAX": str(DEFAULTS["rate_limit_max"]),
        "RATE_LIMIT_WINDOW": str(DEFAULTS["rate_limit_window"]),
        # TUNNEL_ENABLED is read by run.sh; the service itself ignores it.
        "TUNNEL_ENABLED": "1" if tunnel else "0",
    }
    env_path = install_dir / ".env"
    config.write_env_file(env_path, env_values)

    output.info("")
    output.success(f"wrote {env_path} (mode 0600)")
    output.success(f"created {install_dir / 'data'}")
    output.success("generated fresh AUTH_TOKEN")
    if tunnel:
        output.success("Cloudflare tunnel will auto-start on `run.sh`")
        if not shutil.which("cloudflared"):
            output.warn(
                "  cloudflared is NOT installed yet — run.sh will skip the "
                "tunnel until you `brew install cloudflared` (or apt install)."
            )

    # ── 4. Optional: generate a fresh TRON key ───────────────────────────
    if gen_key or (interactive and output.confirm(
        "Generate a fresh TRON private key now?", default=False,
    )):
        output.info("")
        from skr_crypto.cli.commands.keygen import cmd as keygen_cmd
        ctx.invoke(keygen_cmd, write_to=None, yes=True)

    # ── 5. Next-steps ────────────────────────────────────────────────────
    output.info("")
    output.info("[bold]Next steps:[/bold]")
    if key_provider == "env" and not gen_key:
        output.info("  1. Set the private key in your shell:")
        output.info("       export PRIVATE_KEY_HEX='<32-byte-hex>'")
        output.info("     Or generate one: [italic]skr-crypto keygen --write-to env[/italic]")
    elif key_provider == "file":
        output.info("  1. Place the private-key file at the path you'll set "
                    "in PRIVATE_KEY_FILE; chmod 600.")
    elif key_provider == "1password":
        output.info("  1. Make sure `op` CLI is installed and signed in: "
                    "[italic]eval $(op signin)[/italic]")
    elif key_provider == "keychain":
        output.info("  1. Store the key in macOS Keychain (skr-crypto keygen "
                    "--write-to keychain).")

    if not trongrid_api_key:
        output.info("  2. Set TRONGRID_API_KEY in .env (highly recommended).")
    output.info("  3. Start: [italic]./run.sh[/italic]   "
                "(or `skr-crypto start` for systemd / docker)")
    output.info("  4. Verify: [italic]skr-crypto status[/italic]")
    output.info("")
    output.info(f"AUTH_TOKEN (also in .env): [yellow]{auth_token}[/yellow]")
    output.info("Save this — your API clients need it as the X-API-Key header.")


# ---------------------------------------------------------------------------
# resolution helpers — flag value > prompt (if interactive) > default
# ---------------------------------------------------------------------------


def _resolve_choice(
    label: str,
    flag_value: str | None,
    choices: tuple[str, ...],
    default: str,
    interactive: bool,
    *,
    explainer: str | None = None,
) -> str:
    if flag_value is not None:
        return flag_value.lower()
    if not interactive:
        return default
    if explainer:
        output.info(explainer)
    return output.ask_choice(label, choices, default)


def _resolve_string(
    label: str,
    flag_value: str | None,
    default: str,
    interactive: bool,
    *,
    secret: bool = False,
) -> str:
    if flag_value is not None:
        return flag_value
    if not interactive:
        return default
    return output.ask(label, default=default, secret=secret)


def _resolve_bool(
    label: str,
    flag_value: bool | None,
    default: bool,
    interactive: bool,
    *,
    explainer: str | None = None,
) -> bool:
    if flag_value is not None:
        return flag_value
    if not interactive:
        return default
    if explainer:
        output.info(explainer)
    return output.confirm(label + "?", default=default)


def _tunnel_explainer() -> str:
    available = shutil.which("cloudflared") is not None
    note = "available" if available else "NOT installed yet (you can fix later)"
    return (
        f"  Cloudflare quick-tunnel exposes the local API at a temporary\n"
        f"  https://*.trycloudflare.com URL. Useful for webhook testing.\n"
        f"  cloudflared on this machine: {note}"
    )


# Keep the module-level path resolution helper exported for tests.
def install_path(ctx: click.Context) -> Path:  # pragma: no cover - thin shim
    return config.install_dir(ctx.obj.get("install_dir"))
