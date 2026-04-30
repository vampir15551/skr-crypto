"""``skr-crypto wallet ...`` — manage the multi-wallet pool.

Subcommands:

  - ``list``      — list every configured wallet (live balances if the service
                    is up; offline summary otherwise)
  - ``show NAME`` — full info for one wallet
  - ``add NAME``  — add a wallet to the configured backend
  - ``generate NAME`` — generate a fresh key + add as ``NAME``
  - ``remove NAME``   — remove a wallet
  - ``rename OLD NEW`` — rename a wallet
  - ``encrypt`` — convert an existing single-key install to the
                  encrypted-file backend (multi-wallet ready)

Offline behaviour: ``add``/``generate``/``remove``/``rename``/``encrypt``
mutate the on-disk keystore for ``KEY_PROVIDER=encrypted_file`` directly.
For other backends (env/file/keychain/1password) they print
instructions on what to do, since those backends are owned by external
secret stores.

Online behaviour: ``list``/``show`` hit ``/api/v1/wallets`` when the
service is reachable. If it isn't, they fall back to a config-only
summary (names + addresses, no live balances).
"""
from __future__ import annotations

import getpass
import json
import os
import stat
from pathlib import Path

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.api import APIClient
from skr_crypto.cli.exceptions import (
    ServiceUnreachableError,
    SkrCryptoError,
)

# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group("wallet")
def group() -> None:
    """Manage the multi-wallet pool (list, add, generate, remove, ...)."""


# ---------------------------------------------------------------------------
# Helpers — env file + keystore I/O
# ---------------------------------------------------------------------------


def _install_env(ctx: click.Context) -> tuple[Path, dict[str, str]]:
    """Return (install_dir, parsed .env). Raises NotInstalledError."""
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    env = config.read_env_file(install_dir / ".env")
    return install_dir, env


def _provider(env: dict[str, str]) -> str:
    return env.get("KEY_PROVIDER", "1password").strip().lower()


def _keystore_path(install_dir: Path, env: dict[str, str]) -> Path:
    raw = env.get("KEYSTORE_FILE", "")
    if not raw:
        raise SkrCryptoError(
            "KEYSTORE_FILE is not set in .env — KEY_PROVIDER=encrypted_file "
            "requires a path. Run `skr-crypto wallet encrypt` to set up."
        )
    p = Path(raw)
    if not p.is_absolute():
        p = install_dir / p
    return p


def _resolve_passphrase_for_cli(
    env: dict[str, str], *, prompt_label: str = "Keystore passphrase",
) -> bytes:
    """Read the passphrase as the CLI would: env var → file → TTY prompt.

    Differs from server-side ``encrypted_keystore.resolve_passphrase``
    only in that we always allow the TTY prompt (CLI is interactive
    by definition)."""
    pw_env = os.environ.get("KEY_PASSPHRASE", "")
    if pw_env:
        return pw_env.encode("utf-8")
    pw_file = env.get("KEY_PASSPHRASE_FILE") or os.environ.get("KEY_PASSPHRASE_FILE", "")
    if pw_file:
        p = Path(pw_file)
        try:
            st = p.stat()
        except FileNotFoundError:
            raise SkrCryptoError(f"passphrase file {p} not found")
        if st.st_mode & 0o077:
            raise SkrCryptoError(
                f"passphrase file {p} has unsafe mode "
                f"{stat.S_IMODE(st.st_mode):#o} — chmod 600 it"
            )
        data = p.read_bytes().rstrip(b"\r\n")
        if not data:
            raise SkrCryptoError(f"passphrase file {p} is empty")
        return data
    # Interactive prompt
    pw = getpass.getpass(f"{prompt_label}: ")
    if not pw:
        raise SkrCryptoError("empty passphrase")
    return pw.encode("utf-8")


def _import_keystore_module():
    """Lazy import — keystore needs ``cryptography`` from the [server] extra.

    Gives a clean error if the user installed only the bare CLI and
    tries to manipulate an encrypted keystore."""
    try:
        from skr_crypto.server import encrypted_keystore as ks
    except ImportError as exc:
        raise SkrCryptoError(
            "encrypted-file keystore commands require `cryptography` — "
            "run `pip install 'skr-crypto[server]'`."
        ) from exc
    return ks


def _derive_address_from_hex(hex_key: str) -> str:
    """Derive the TRON address from a hex private key. Lazy-imports tronpy."""
    try:
        from tronpy.keys import PrivateKey
    except ImportError as exc:
        raise SkrCryptoError(
            "tronpy is not installed in the CLI's venv — "
            "run `pip install 'skr-crypto[server]'`."
        ) from exc
    return PrivateKey(bytes.fromhex(hex_key)).public_key.to_base58check_address()


# ---------------------------------------------------------------------------
# `wallet list`  —  online listing with live balances; falls back offline
# ---------------------------------------------------------------------------


@group.command("list")
@click.option(
    "--offline", is_flag=True,
    help="Skip the live /wallets call; show only the configured names + addresses.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def list_cmd(ctx: click.Context, offline: bool, as_json: bool) -> None:
    """List every configured wallet."""
    install_dir, env = _install_env(ctx)

    if not offline:
        try:
            client = APIClient.from_env_file(install_dir / ".env")
            payload = client.wallets()
        except ServiceUnreachableError:
            output.warn(
                "Service is not reachable — falling back to offline listing."
            )
            payload = None
        except Exception as exc:
            output.warn(f"Online listing failed ({exc}) — falling back offline.")
            payload = None
    else:
        payload = None

    if payload is not None:
        if as_json:
            output.print_json(payload)
            return
        rows = payload.get("wallets") or []
        auto = payload.get("auto_pick") or "?"
        table = output.make_table(
            "Wallet", "Address", "USDT", "TRX", "Energy",
            title=f"Wallets (auto-pick: {auto})",
        )
        for r in rows:
            table.add_row(
                r.get("wallet", "?"),
                r.get("address", "?"),
                r.get("usdt", "?"),
                r.get("trx", "?"),
                str(r.get("energy_available", 0)),
            )
        output.render_table(table)
        return

    # Offline path: enumerate names from .env / keystore.
    names = _offline_names(install_dir, env)
    if as_json:
        output.print_json({"wallets": [{"wallet": n} for n in names], "online": False})
        return
    if not names:
        output.warn("No wallets configured.")
        return
    table = output.make_table("Wallet", title="Wallets (offline)")
    for n in names:
        table.add_row(n)
    output.render_table(table)


def _offline_names(install_dir: Path, env: dict[str, str]) -> list[str]:
    """Best-effort enumeration of wallet names without hitting the server."""
    provider = _provider(env)
    explicit = (env.get("WALLETS") or "").strip()

    if provider == "encrypted_file":
        path = _keystore_path(install_dir, env)
        if not path.exists():
            return []
        ks = _import_keystore_module()
        try:
            return ks.list_wallet_names(path)
        except Exception as exc:
            output.warn(f"Could not read keystore: {exc}")
            return []

    if explicit:
        return [n.strip() for n in explicit.split(",") if n.strip()]

    # Single-wallet legacy backends → just "default".
    return ["default"]


# ---------------------------------------------------------------------------
# `wallet show NAME`
# ---------------------------------------------------------------------------


@group.command("show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def show_cmd(ctx: click.Context, name: str, as_json: bool) -> None:
    """Show one wallet (live balance + address)."""
    install_dir, _ = _install_env(ctx)
    client = APIClient.from_env_file(install_dir / ".env")
    payload = client.balance(wallet=name)
    if as_json:
        output.print_json(payload)
        return
    table = output.make_table("Field", "Value", title=f"Wallet {name}")
    for k, v in payload.items():
        table.add_row(str(k), str(v))
    output.render_table(table)


# ---------------------------------------------------------------------------
# `wallet add NAME`
# ---------------------------------------------------------------------------


@group.command("add")
@click.argument("name")
@click.option(
    "--hex", "hex_key",
    help="32-byte private key as 64 hex chars (no 0x). If omitted, prompted.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
@click.pass_context
def add_cmd(
    ctx: click.Context, name: str, hex_key: str | None, yes: bool,
) -> None:
    """Add an existing TRON private key to the pool as wallet ``NAME``.

    For ``KEY_PROVIDER=encrypted_file`` the key is added to the keystore
    in place. For other backends the command prints the env var or file
    layout you need to apply yourself.
    """
    install_dir, env = _install_env(ctx)
    _validate_name(name)

    if hex_key is None:
        hex_key = getpass.getpass(f"Private key (hex, 64 chars) for {name}: ")
    hex_key = hex_key.strip().lower().removeprefix("0x")
    _validate_hex_key(hex_key)
    address = _derive_address_from_hex(hex_key)
    output.info(f"Derived address: {address}")
    if not yes and not output.confirm(f"Add wallet {name!r}?", default=True):
        output.info("Aborted.")
        ctx.exit(1)

    provider = _provider(env)
    if provider == "encrypted_file":
        _add_to_encrypted_file(install_dir, env, name, hex_key, address)
        return
    _print_add_instructions(provider, name, hex_key, address)


def _add_to_encrypted_file(
    install_dir: Path, env: dict[str, str],
    name: str, hex_key: str, address: str,
) -> None:
    ks = _import_keystore_module()
    path = _keystore_path(install_dir, env)
    if not path.exists():
        raise SkrCryptoError(
            f"keystore {path} does not exist — initialise it with "
            f"`skr-crypto wallet encrypt` first"
        )
    passphrase = _resolve_passphrase_for_cli(env)
    entry = ks.KeystoreEntry(
        name=name,
        address=address,
        raw_key=bytearray.fromhex(hex_key),
    )
    try:
        ks.add_wallet(path, passphrase, entry)
    except ks.WalletNameConflict as exc:
        raise SkrCryptoError(str(exc)) from exc
    except ks.WrongPassphrase:
        raise SkrCryptoError("passphrase rejected — keystore was not modified")
    except ks.KeystoreError as exc:
        raise SkrCryptoError(f"keystore error: {exc}") from exc
    output.success(f"Added wallet {name!r} → {address}")
    output.info(
        "Restart the service for the new wallet to become signable: "
        "`skr-crypto restart`."
    )


def _print_add_instructions(
    provider: str, name: str, hex_key: str, address: str,
) -> None:
    """Tell the operator how to register the new wallet for backends we
    don't write to directly."""
    output.info(f"Address: {address}")
    if provider == "env":
        var = f"WALLET_{name.upper().replace('-', '_')}_PRIVATE_KEY_HEX"
        output.print_value(f"export {var}={hex_key}")
        output.info(
            f"Append [italic]{name}[/italic] to WALLETS in .env "
            f"(comma-separated), then restart."
        )
    elif provider == "file":
        var = f"WALLET_{name.upper().replace('-', '_')}_PRIVATE_KEY_FILE"
        suggestion = f"~/.skr-crypto/keys/{name}.key"
        output.info(
            f"Save the hex key to {suggestion} (chmod 600), then add to .env:"
        )
        output.print_value(f"WALLETS={name}")
        output.print_value(f"{var}={suggestion}")
        output.info("Restart the service.")
    elif provider == "keychain":
        output.info("Run:")
        output.print_value(
            f"security add-generic-password -s payouts -a {name} "
            f"-w '{hex_key}' -U"
        )
        var = f"WALLET_{name.upper().replace('-', '_')}_KEYCHAIN_ACCOUNT"
        output.info("Then in .env:")
        output.print_value(f"WALLETS={name}")
        output.print_value(f"{var}={name}")
        output.info("Restart the service.")
    elif provider == "1password":
        output.info(
            "Add a 1Password item containing this key, then update .env:"
        )
        output.print_value(f"WALLETS={name}")
        output.print_value(
            f"WALLET_{name.upper().replace('-', '_')}_OP_ITEM=<the-1password-item-name>"
        )
        output.info("Restart the service.")
    else:
        raise SkrCryptoError(f"Unsupported KEY_PROVIDER for `wallet add`: {provider}")


# ---------------------------------------------------------------------------
# `wallet generate NAME`
# ---------------------------------------------------------------------------


@group.command("generate")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True)
@click.pass_context
def generate_cmd(ctx: click.Context, name: str, yes: bool) -> None:
    """Generate a fresh TRON private key and add it as wallet ``NAME``."""
    _validate_name(name)
    output.warn(
        "[bold]This generates a fresh TRON private key.[/bold] "
        "It is shown / persisted exactly once."
    )
    if not yes and not output.confirm("Generate now?", default=False):
        output.info("Aborted.")
        ctx.exit(1)
    try:
        from tronpy.keys import PrivateKey
    except ImportError as exc:
        raise SkrCryptoError(
            "tronpy is not installed — run `pip install 'skr-crypto[server]'`"
        ) from exc
    pk = PrivateKey.random()
    hex_key = pk.hex()
    address = pk.public_key.to_base58check_address()
    output.info(f"Address: [bold]{address}[/bold]")

    install_dir, env = _install_env(ctx)
    provider = _provider(env)
    if provider == "encrypted_file":
        _add_to_encrypted_file(install_dir, env, name, hex_key, address)
        return
    output.print_value(f"PRIVATE_KEY_HEX={hex_key}")
    output.info("Save the key NOW — it isn't stored on disk by this command.")
    _print_add_instructions(provider, name, hex_key, address)


# ---------------------------------------------------------------------------
# `wallet remove NAME`
# ---------------------------------------------------------------------------


@group.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True)
@click.pass_context
def remove_cmd(ctx: click.Context, name: str, yes: bool) -> None:
    """Remove a wallet from the pool. Cannot be undone — back up first."""
    install_dir, env = _install_env(ctx)
    provider = _provider(env)
    if not yes and not output.confirm(
        f"Remove wallet {name!r}? This cannot be undone.", default=False,
    ):
        output.info("Aborted.")
        ctx.exit(1)
    if provider == "encrypted_file":
        ks = _import_keystore_module()
        path = _keystore_path(install_dir, env)
        passphrase = _resolve_passphrase_for_cli(env)
        try:
            ks.remove_wallet(path, passphrase, name)
        except ks.WrongPassphrase:
            raise SkrCryptoError("passphrase rejected — keystore not modified")
        except ks.KeystoreError as exc:
            raise SkrCryptoError(str(exc)) from exc
        output.success(f"Removed wallet {name!r}")
        output.info("Restart the service for the change to take effect.")
        return
    output.warn(
        f"KEY_PROVIDER={provider} is not directly editable. Update your "
        f"secret store + remove {name!r} from WALLETS in .env, then restart."
    )


# ---------------------------------------------------------------------------
# `wallet rename OLD NEW`
# ---------------------------------------------------------------------------


@group.command("rename")
@click.argument("old")
@click.argument("new")
@click.pass_context
def rename_cmd(ctx: click.Context, old: str, new: str) -> None:
    """Rename a wallet (encrypted_file backend only)."""
    _validate_name(new)
    install_dir, env = _install_env(ctx)
    provider = _provider(env)
    if provider != "encrypted_file":
        raise SkrCryptoError(
            f"`wallet rename` is supported only for KEY_PROVIDER=encrypted_file "
            f"(current: {provider}). Edit your secret store + WALLETS by hand."
        )
    ks = _import_keystore_module()
    path = _keystore_path(install_dir, env)
    passphrase = _resolve_passphrase_for_cli(env)
    try:
        ks.rename_wallet(path, passphrase, old, new)
    except ks.WrongPassphrase:
        raise SkrCryptoError("passphrase rejected — keystore not modified")
    except ks.KeystoreError as exc:
        raise SkrCryptoError(str(exc)) from exc
    output.success(f"Renamed {old!r} → {new!r}")


# ---------------------------------------------------------------------------
# `wallet encrypt`  —  one-shot migration to encrypted_file
# ---------------------------------------------------------------------------


@group.command("encrypt")
@click.option(
    "--output-path", "-o", "output_path",
    default=None,
    help="Where to write the keystore (default: data/keystore.json under install dir).",
)
@click.option(
    "--from-hex", "from_hex",
    default=None,
    help="Source: a single hex private key (typed once). Mutually exclusive with --from-env.",
)
@click.option(
    "--from-env", "from_env_var",
    default=None,
    help="Source: read hex from the named env var (e.g. PRIVATE_KEY_HEX).",
)
@click.option(
    "--name", "wallet_name",
    default="default",
    show_default=True,
    help="Wallet name to use inside the new keystore.",
)
@click.option(
    "--force", is_flag=True,
    help="Overwrite an existing keystore at the destination.",
)
@click.pass_context
def encrypt_cmd(
    ctx: click.Context,
    output_path: str | None,
    from_hex: str | None,
    from_env_var: str | None,
    wallet_name: str,
    force: bool,
) -> None:
    """Initialise an encrypted-file keystore and (optionally) seed it
    with one wallet.

    After running this, set in your .env::

        KEY_PROVIDER=encrypted_file
        KEYSTORE_FILE=<path-printed-by-this-command>
        KEY_PASSPHRASE=<from-secret-manager>     # OR
        KEY_PASSPHRASE_FILE=<chmod-600-file>     # OR provide on stdin

    and restart. Subsequent ``skr-crypto wallet add`` calls write here.
    """
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    ks = _import_keystore_module()

    out_path = Path(output_path) if output_path else (install_dir / "data" / "keystore.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and not force:
        raise SkrCryptoError(
            f"keystore already exists at {out_path}. Pass --force to overwrite."
        )

    # Passphrase: ask twice for confirmation. We never echo and never
    # show even the length back to the operator.
    pw1 = getpass.getpass("New keystore passphrase (will not echo): ")
    if not pw1:
        raise SkrCryptoError("empty passphrase")
    pw2 = getpass.getpass("Confirm passphrase: ")
    if pw1 != pw2:
        raise SkrCryptoError("passphrases do not match — aborting")
    passphrase = pw1.encode("utf-8")

    # Resolve source key (optional).
    hex_key: str | None = None
    if from_hex and from_env_var:
        raise SkrCryptoError("--from-hex and --from-env are mutually exclusive")
    if from_hex:
        hex_key = from_hex.strip().lower().removeprefix("0x")
    elif from_env_var:
        v = os.environ.get(from_env_var, "").strip().lower().removeprefix("0x")
        if not v:
            raise SkrCryptoError(f"env var {from_env_var} is empty/unset")
        hex_key = v
    if hex_key is not None:
        _validate_hex_key(hex_key)

    # Initialise + populate.
    ks.init_keystore(out_path, passphrase, overwrite=force)
    if hex_key:
        _validate_name(wallet_name)
        address = _derive_address_from_hex(hex_key)
        ks.add_wallet(
            out_path, passphrase,
            ks.KeystoreEntry(
                name=wallet_name,
                address=address,
                raw_key=bytearray.fromhex(hex_key),
            ),
        )
        output.success(
            f"Created keystore {out_path} with wallet {wallet_name!r} → {address}"
        )
    else:
        output.success(f"Created empty keystore {out_path}")
        output.info(
            "Add wallets with `skr-crypto wallet add NAME` "
            "(or `wallet generate NAME` for a fresh key)."
        )

    output.info(
        "\nNext: edit your .env to use the new keystore:\n"
        f"  KEY_PROVIDER=encrypted_file\n"
        f"  KEYSTORE_FILE={out_path}\n"
        "  # then either\n"
        "  KEY_PASSPHRASE=...           # via secret manager / docker secret\n"
        "  # or\n"
        "  KEY_PASSPHRASE_FILE=...      # chmod 600 file\n"
        "Then `skr-crypto restart`."
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> None:
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise SkrCryptoError(
            f"invalid wallet name {name!r} — use letters/digits/_/- only"
        )


def _validate_hex_key(h: str) -> None:
    if len(h) != 64:
        raise SkrCryptoError(
            f"private key must be 64 hex chars (got {len(h)})"
        )
    try:
        b = bytes.fromhex(h)
    except ValueError as exc:
        raise SkrCryptoError(f"private key is not valid hex: {exc}") from exc
    if len(b) != 32:
        raise SkrCryptoError(
            f"private key decoded to {len(b)} bytes (expected 32)"
        )
    if all(byte == 0 for byte in b):
        raise SkrCryptoError("private key is all zeroes — refusing")


# ---------------------------------------------------------------------------
# Re-export so cli.main imports `wallet.group` cleanly
# ---------------------------------------------------------------------------

# Convenience alias so `from skr_crypto.cli.commands import wallet; wallet.cmd`
# matches the pattern used by other CLI modules.
cmd = group


# Silence "unused" warnings for json / Path that show up only in some
# code paths.
_ = (json, Path)
