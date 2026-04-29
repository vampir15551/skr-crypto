"""``skr-crypto install`` — bootstrap a service install dir.

Two modes share one code path:

  - Interactive (default when STDIN is a TTY) walks a numbered
    wizard. Per-provider follow-ups are inserted right after the
    Key-provider step so the prompts are always linear / numbered.

  - Non-interactive (``--yes`` or no TTY) accepts whatever flags
    were passed and falls through to defaults for the rest. Used by
    CI / automation. ``--interactive`` forces the wizard even on
    non-TTY (e.g. for testing).

What the command writes::

    <install-dir>/
        .env            chmod 600, all settings
        data/           audit + idempotency state goes here

If ``--gen-key`` (or ``Yes`` to the gen-key prompt), it also runs
``skr-crypto keygen`` after writing ``.env``.
"""
from __future__ import annotations

import secrets
import shutil
import sys

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.exceptions import AlreadyInstalledError

# ---------------------------------------------------------------------------
# Defaults — single source of truth, used as both flag defaults and
# wizard-prompt defaults.
# ---------------------------------------------------------------------------

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
    # Per-provider extras
    "key_file": "/etc/skr-crypto/treasury.key",
    "op_vault": "Treasury",
    "op_item": "TRON-Treasury",
    "op_field": "password",
    "keychain_service": "skr-crypto",
    "keychain_account": "treasury",
}

KEY_PROVIDER_OPTIONS = [
    ("env",       "PRIVATE_KEY_HEX from process env (containers, CI)"),
    ("file",      "chmod-600 file at PRIVATE_KEY_FILE (VPS / systemd)"),
    ("1password", "1Password CLI (op); operator-on-laptop"),
    ("keychain",  "macOS Keychain (local dev)"),
]
NETWORK_OPTIONS = [
    ("mainnet", "production TRON"),
    ("shasta",  "testnet — for dry-runs"),
    ("nile",    "testnet — for dry-runs"),
]
KEY_PROVIDERS = tuple(v for v, _ in KEY_PROVIDER_OPTIONS)
NETWORKS = tuple(v for v, _ in NETWORK_OPTIONS)


@click.command("install")
# Mode flags --------------------------------------------------------------
@click.option("-y", "--yes", is_flag=True,
              help="Non-interactive: accept defaults / explicit flags.")
@click.option("--interactive/--no-interactive", default=None,
              help="Force the wizard on/off. Default: wizard if STDIN is a "
                   "TTY and --yes was not given.")
@click.option("--force", is_flag=True,
              help="Wipe an existing install dir before installing.")
# Per-setting flags -------------------------------------------------------
@click.option("--key-provider",
              type=click.Choice(KEY_PROVIDERS, case_sensitive=False),
              default=None,
              help="How the service loads the private key. "
                   f"Default: {DEFAULTS['key_provider']}.")
@click.option("--key-file", default=None,
              help="(KEY_PROVIDER=file) chmod-600 file with the hex key. "
                   f"Default: {DEFAULTS['key_file']}.")
@click.option("--op-vault", default=None, help="(KEY_PROVIDER=1password) vault name.")
@click.option("--op-item", default=None, help="(KEY_PROVIDER=1password) item name.")
@click.option("--op-field", default=None, help="(KEY_PROVIDER=1password) field name.")
@click.option("--keychain-service", default=None,
              help=f"(KEY_PROVIDER=keychain) service name. Default: {DEFAULTS['keychain_service']}.")
@click.option("--keychain-account", default=None,
              help=f"(KEY_PROVIDER=keychain) account name. Default: {DEFAULTS['keychain_account']}.")
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
@click.option("--advanced", is_flag=True,
              help="Prompt for advanced knobs (TRX reserve, energy cap, "
                   "shutdown timeout, rate limit). Otherwise sane defaults "
                   "are used and tweakable later via `skr-crypto config edit`.")
@click.pass_context
def cmd(ctx: click.Context, **opts) -> None:
    """Initialise the runtime state directory for the service."""
    install_dir = config.install_dir(ctx.obj.get("install_dir"))

    # Mode: explicit flag wins, else --yes, else auto-detect TTY.
    interactive = opts["interactive"]
    if interactive is None:
        interactive = (not opts["yes"]) and sys.stdin.isatty()

    # ── 0. Refuse / wipe existing install ────────────────────────────────
    already_installed = install_dir.exists() and (install_dir / ".env").exists()
    if already_installed and not opts["force"]:
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

    # ── 1. Resolve every setting (flag > prompt > default) ──────────────
    # Steps are numbered so the operator always knows where they are.
    settings = _collect_settings(opts, interactive=interactive)

    # ── 2. Write .env ───────────────────────────────────────────────────
    auth_token = secrets.token_urlsafe(32)
    env_values = _env_values_from(settings, auth_token)
    env_path = install_dir / ".env"
    config.write_env_file(env_path, env_values)

    output.info("")
    output.success(f"wrote {env_path} (mode 0600)")
    output.success(f"created {install_dir / 'data'}")
    output.success("generated fresh AUTH_TOKEN")
    if settings["tunnel"]:
        if shutil.which("cloudflared"):
            output.success("Cloudflare tunnel will auto-start on `run.sh`")
        else:
            output.warn(
                "Cloudflare tunnel enabled, but cloudflared is NOT installed "
                "yet — run.sh will skip the tunnel until you "
                "`brew install cloudflared` (or apt install)."
            )

    # ── 3. Optional: generate a fresh TRON key ──────────────────────────
    if settings["gen_key"]:
        output.info("")
        from skr_crypto.cli.commands.keygen import cmd as keygen_cmd
        ctx.invoke(keygen_cmd, write_to=None, yes=True)

    # ── 4. Next-steps ───────────────────────────────────────────────────
    output.info("")
    output.info("[bold]Next steps:[/bold]")
    _print_next_steps(settings, gen_key_done=settings["gen_key"])
    output.info("")
    output.info(f"AUTH_TOKEN (also in .env): [yellow]{auth_token}[/yellow]")
    output.info("Save this — your API clients need it as the X-API-Key header.")


# ---------------------------------------------------------------------------
# Wizard plumbing
# ---------------------------------------------------------------------------


def _collect_settings(opts: dict, *, interactive: bool) -> dict:
    """Walk every prompt; return a settled-values dict.

    The order of resolution per setting is always:
      1. explicit flag (opts["..."] is not None) — wins
      2. interactive prompt — if interactive
      3. DEFAULTS[...]
    """
    n_steps = 7 + (1 if opts["advanced"] else 0)

    s: dict = {}

    # ── Step 1: key provider ─────────────────────────────────────────────
    s["key_provider"] = _resolve_choice_numbered(
        opts["key_provider"], interactive,
        label="Key provider — how the service loads the TRON private key",
        options=KEY_PROVIDER_OPTIONS,
        default=DEFAULTS["key_provider"],
        step=f"[1/{n_steps}]",
    )

    # ── Step 1.5: per-provider follow-ups ────────────────────────────────
    s["key_file"] = ""
    s["op_vault"] = s["op_item"] = s["op_field"] = ""
    s["keychain_service"] = s["keychain_account"] = ""

    if s["key_provider"] == "file":
        s["key_file"] = _resolve_string(
            opts["key_file"], interactive,
            label="Path to the chmod-600 file with the hex private key",
            default=DEFAULTS["key_file"],
            step=f"[1.5/{n_steps}]",
        )
    elif s["key_provider"] == "1password":
        s["op_vault"] = _resolve_string(
            opts["op_vault"], interactive,
            label="1Password vault name",
            default=DEFAULTS["op_vault"],
            step=f"[1.5/{n_steps}]",
        )
        s["op_item"] = _resolve_string(
            opts["op_item"], interactive,
            label="1Password item name",
            default=DEFAULTS["op_item"],
            step=f"[1.6/{n_steps}]",
        )
        s["op_field"] = _resolve_string(
            opts["op_field"], interactive,
            label="1Password field name",
            default=DEFAULTS["op_field"],
            step=f"[1.7/{n_steps}]",
        )
    elif s["key_provider"] == "keychain":
        s["keychain_service"] = _resolve_string(
            opts["keychain_service"], interactive,
            label="macOS Keychain service name",
            default=DEFAULTS["keychain_service"],
            step=f"[1.5/{n_steps}]",
        )
        s["keychain_account"] = _resolve_string(
            opts["keychain_account"], interactive,
            label="macOS Keychain account name",
            default=DEFAULTS["keychain_account"],
            step=f"[1.6/{n_steps}]",
        )

    # ── Step 2: TRON network ─────────────────────────────────────────────
    s["network"] = _resolve_choice_numbered(
        opts["network"], interactive,
        label="TRON network",
        options=NETWORK_OPTIONS,
        default=DEFAULTS["network"],
        step=f"[2/{n_steps}]",
    )

    # ── Step 3: TronGrid API key ─────────────────────────────────────────
    s["trongrid_api_key"] = _resolve_string(
        opts["trongrid_api_key"], interactive,
        label="TronGrid API key (recommended; blank to skip)",
        default=DEFAULTS["trongrid_api_key"],
        step=f"[3/{n_steps}]",
        secret=True,
    )

    # ── Step 4: bind host / port ─────────────────────────────────────────
    s["bind_host"] = _resolve_string(
        opts["bind_host"], interactive,
        label="Bind host",
        default=DEFAULTS["bind_host"],
        step=f"[4/{n_steps}]",
    )
    bind_port_str = _resolve_string(
        str(opts["bind_port"]) if opts["bind_port"] else None, interactive,
        label="Bind port",
        default=str(DEFAULTS["bind_port"]),
        step=f"[4/{n_steps}]",
    )
    try:
        s["bind_port"] = int(bind_port_str)
    except ValueError:
        output.warn(
            f"Bind port {bind_port_str!r} is not an integer; using default "
            f"{DEFAULTS['bind_port']}."
        )
        s["bind_port"] = DEFAULTS["bind_port"]

    # ── Step 5: tunnel ───────────────────────────────────────────────────
    cf = "available" if shutil.which("cloudflared") else "NOT installed yet"
    s["tunnel"] = _resolve_bool(
        opts["tunnel"], interactive,
        label=f"Cloudflare quick-tunnel? — temporary public https URL on `run.sh` "
              f"(cloudflared on this machine: {cf})",
        default=DEFAULTS["tunnel"],
        step=f"[5/{n_steps}]",
    )

    # ── Step 6: generate fresh TRON key now? ─────────────────────────────
    if opts["gen_key"]:
        s["gen_key"] = True
    elif interactive:
        s["gen_key"] = output.ask_yes_no(
            "Generate a fresh TRON private key now?",
            default=False,
            step=f"[6/{n_steps}]",
        )
    else:
        s["gen_key"] = False

    # ── Step 7: advanced knobs (only if --advanced or asked) ─────────────
    s["min_trx_reserve"] = DEFAULTS["min_trx_reserve"]
    s["max_energy_burn_trx"] = DEFAULTS["max_energy_burn_trx"]
    s["shutdown_timeout"] = DEFAULTS["shutdown_timeout"]
    s["rate_limit_max"] = DEFAULTS["rate_limit_max"]
    s["rate_limit_window"] = DEFAULTS["rate_limit_window"]

    show_advanced = opts["advanced"]
    if interactive and not show_advanced:
        show_advanced = output.ask_yes_no(
            "Tweak advanced settings (TRX reserve / energy cap / shutdown / rate limit)?",
            default=False,
            step=f"[7/{n_steps}]",
        )

    if show_advanced and interactive:
        output.info("")
        output.info("[bold]Advanced[/bold] — Enter to keep each default")
        s["min_trx_reserve"] = output.ask(
            "MIN_TRX_RESERVE — refuse /send if treasury TRX < this (TRX)",
            default=DEFAULTS["min_trx_reserve"],
        )
        s["max_energy_burn_trx"] = output.ask(
            "MAX_ENERGY_BURN_TRX — reject /send if estimate > this (TRX, 0 disables)",
            default=DEFAULTS["max_energy_burn_trx"],
        )
        s["shutdown_timeout"] = int(output.ask(
            "SHUTDOWN_TIMEOUT — auto-shutdown after N idle seconds",
            default=str(DEFAULTS["shutdown_timeout"]),
        ) or DEFAULTS["shutdown_timeout"])
        s["rate_limit_max"] = int(output.ask(
            "RATE_LIMIT_MAX — max requests per window (per IP)",
            default=str(DEFAULTS["rate_limit_max"]),
        ) or DEFAULTS["rate_limit_max"])
        s["rate_limit_window"] = int(output.ask(
            "RATE_LIMIT_WINDOW — window size (seconds)",
            default=str(DEFAULTS["rate_limit_window"]),
        ) or DEFAULTS["rate_limit_window"])

    return s


def _resolve_choice_numbered(
    flag_value: str | None,
    interactive: bool,
    *,
    label: str,
    options: list[tuple[str, str]],
    default: str,
    step: str,
) -> str:
    if flag_value is not None:
        return flag_value.lower()
    if not interactive:
        return default
    return output.ask_choice_numbered(
        label, options, default_value=default, step=step,
    )


def _resolve_string(
    flag_value: str | None,
    interactive: bool,
    *,
    label: str,
    default: str,
    step: str | None = None,
    secret: bool = False,
) -> str:
    if flag_value is not None:
        return flag_value
    if not interactive:
        return default
    if step:
        output.info(f"\n[bold cyan]{step}[/bold cyan] {label}")
        return output.ask("Value", default=default, secret=secret)
    return output.ask(label, default=default, secret=secret)


def _resolve_bool(
    flag_value: bool | None,
    interactive: bool,
    *,
    label: str,
    default: bool,
    step: str,
) -> bool:
    if flag_value is not None:
        return flag_value
    if not interactive:
        return default
    return output.ask_yes_no(label, default=default, step=step)


def _env_values_from(s: dict, auth_token: str) -> dict[str, str]:
    """Map the wizard's settled state into the literal .env keys."""
    values: dict[str, str] = {
        "AUTH_TOKEN": auth_token,
        "TRON_NETWORK": s["network"],
        "TRONGRID_API_KEY": s["trongrid_api_key"],
        "USDT_CONTRACT": DEFAULTS["usdt_contract"],
        "KEY_PROVIDER": s["key_provider"],
        "MIN_TRX_RESERVE": str(s["min_trx_reserve"]),
        "MAX_ENERGY_BURN_TRX": str(s["max_energy_burn_trx"]),
        "AUDIT_LOG_FILE": DEFAULTS["audit_log"],
        "IDEMPOTENCY_DB_PATH": DEFAULTS["idempotency_db"],
        "SERVER_HOST": s["bind_host"],
        "SERVER_PORT": str(s["bind_port"]),
        "SHUTDOWN_TIMEOUT": str(s["shutdown_timeout"]),
        "RATE_LIMIT_MAX": str(s["rate_limit_max"]),
        "RATE_LIMIT_WINDOW": str(s["rate_limit_window"]),
        # TUNNEL_ENABLED is read by run.sh; the service itself ignores it.
        "TUNNEL_ENABLED": "1" if s["tunnel"] else "0",
    }
    # Per-provider extras — only emit the ones that matter, so the .env
    # stays small and the unused defaults aren't misleading.
    if s["key_provider"] == "file" and s["key_file"]:
        values["PRIVATE_KEY_FILE"] = s["key_file"]
    if s["key_provider"] == "1password":
        if s["op_vault"]:
            values["OP_VAULT"] = s["op_vault"]
        if s["op_item"]:
            values["OP_ITEM"] = s["op_item"]
        if s["op_field"]:
            values["OP_FIELD"] = s["op_field"]
    if s["key_provider"] == "keychain":
        if s["keychain_service"]:
            values["KEYCHAIN_SERVICE"] = s["keychain_service"]
        if s["keychain_account"]:
            values["KEYCHAIN_ACCOUNT"] = s["keychain_account"]
    return values


def _print_next_steps(s: dict, *, gen_key_done: bool) -> None:
    output.info("  1. Set the private key:")
    if s["key_provider"] == "env" and not gen_key_done:
        output.info("       export PRIVATE_KEY_HEX='<32-byte-hex>'")
        output.info("     Or generate one: [italic]skr-crypto keygen --write-to env[/italic]")
    elif s["key_provider"] == "file":
        kf = s["key_file"] or DEFAULTS["key_file"]
        output.info(f"     Place the hex key at [italic]{kf}[/italic] (chmod 600).")
        if not gen_key_done:
            output.info(f"     Or generate one: [italic]skr-crypto keygen "
                        f"--write-to file --path {kf}[/italic]")
    elif s["key_provider"] == "1password":
        output.info("     Sign in to 1Password CLI: [italic]eval $(op signin)[/italic]")
        output.info(f"     Make sure the field {s['op_field']!r} of item "
                    f"{s['op_item']!r} in vault {s['op_vault']!r} holds the "
                    f"hex key.")
    elif s["key_provider"] == "keychain":
        if not gen_key_done:
            output.info(
                f"     Generate + store: [italic]skr-crypto keygen "
                f"--write-to keychain --keychain-service {s['keychain_service']} "
                f"--keychain-account {s['keychain_account']}[/italic]"
            )
        else:
            output.info("     (Done — key already in macOS Keychain.)")

    if not s["trongrid_api_key"]:
        output.info("  2. Set TRONGRID_API_KEY in .env (highly recommended).")
    output.info("  3. Start: [italic]./run.sh[/italic]   "
                "(or `skr-crypto start` for systemd / docker)")
    output.info("  4. Verify: [italic]skr-crypto status[/italic]")
