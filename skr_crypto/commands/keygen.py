"""``skr-crypto keygen`` — generate a fresh TRON private key.

Why a separate command (and not a flag on ``install``):

  - Generation is a one-way action with high operator risk: lose the
    output and you lose access to whatever you fund the address with.
    Pulling it out into its own command makes the action explicit
    instead of buried under an install flag.
  - Operators sometimes need to rotate the key on an existing install,
    not just at first-time setup.

What it does:

  1. Generates a fresh 32-byte private key via ``tronpy.keys.PrivateKey.random``.
  2. Derives the TRON address.
  3. Either prints the key + address (default), or writes it to the
     selected backend (``--write-to env|file|keychain``).
  4. The 1Password backend is **never** auto-written — the operator
     copies into 1Password by hand. We don't shell out to ``op``
     write commands; if anything goes wrong mid-write the key would
     be in shell history.

The hex key is shown **once and only once**. We refuse to save it to
disk in any path other than the explicitly-requested chmod-600 file.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import click

from skr_crypto import config, output
from skr_crypto.exceptions import NotInstalledError, SkrCryptoError

# Where to write — these are the same names as KEY_PROVIDER values.
WRITE_TO_CHOICES = ("none", "env", "file", "keychain")


@click.command("keygen")
@click.option(
    "--write-to",
    type=click.Choice(WRITE_TO_CHOICES),
    default=None,
    help="Where to put the generated key. Default: print only "
         "(operator copies manually). 'none' is an alias for the "
         "default. Auto-detects from the installed service's "
         "KEY_PROVIDER if not given and an install is present.",
)
@click.option(
    "--path", "file_path",
    type=click.Path(dir_okay=False),
    help="Required with --write-to file. The hex key is written here "
         "with chmod 600. We refuse to overwrite an existing file.",
)
@click.option(
    "--keychain-service", default="payouts", show_default=True,
    help="(macOS) Keychain `-s` service name when --write-to keychain.",
)
@click.option(
    "--keychain-account", default="treasury", show_default=True,
    help="(macOS) Keychain `-a` account name when --write-to keychain.",
)
@click.option(
    "--yes", "-y", is_flag=True,
    help="Skip the 'I understand' confirmation prompt.",
)
@click.pass_context
def cmd(
    ctx: click.Context,
    write_to: str | None,
    file_path: str | None,
    keychain_service: str,
    keychain_account: str,
    yes: bool,
) -> None:
    """Generate a fresh TRON private key.

    The generated value is shown once. There is no way to recover it
    if you lose the output of this command.
    """
    # Resolve --write-to: explicit flag → service .env → 'none'
    chosen = write_to or _autodetect_provider(ctx) or "none"

    # Sanity: file backend needs a path.
    if chosen == "file" and not file_path:
        raise SkrCryptoError(
            "--write-to file requires --path PATH (chmod-600 file)"
        )

    output.warn("[bold]This generates a fresh TRON private key.[/bold]")
    output.warn(
        "Lose it and you lose access to whatever address it funds. "
        "It is shown ONCE."
    )
    if not yes and not output.confirm(
        "Generate now?", default=False,
    ):
        output.info("Aborted.")
        ctx.exit(1)

    # Lazy-import tronpy: only this command needs it, and the CLI
    # itself shouldn't carry the dep weight.
    try:
        from tronpy.keys import PrivateKey
    except ImportError as exc:
        raise SkrCryptoError(
            "tronpy is not installed in the CLI's venv. Either install it "
            "(`pip install tronpy`) or run `skr-crypto keygen` inside the "
            "service's venv where tronpy is already a dep."
        ) from exc

    pk = PrivateKey.random()
    hex_key = pk.hex()
    address = pk.public_key.to_base58check_address()

    output.info("")
    output.print_value(f"address:    [bold]{address}[/bold]")

    if chosen == "none":
        output.print_value(f"PRIVATE_KEY_HEX={hex_key}")
        output.info("")
        output.info(
            "Save the hex value securely. Future runs of skr-crypto "
            "won't show it — it isn't stored on disk by this command."
        )
        return

    if chosen == "env":
        output.print_value(f"PRIVATE_KEY_HEX={hex_key}")
        output.info("")
        output.info(
            "Run [italic]export PRIVATE_KEY_HEX=...[/italic] from your "
            "shell, or wire it into your secret manager. Don't put it "
            "in .env — that's why KEY_PROVIDER=env reads from the "
            "environment, not the dotfile."
        )
        return

    if chosen == "file":
        _write_file_backend(file_path, hex_key, address)
        return

    if chosen == "keychain":
        _write_keychain_backend(
            keychain_service, keychain_account, hex_key, address,
        )
        return

    # 1password is intentionally not in WRITE_TO_CHOICES.
    raise SkrCryptoError(f"Unsupported --write-to {chosen!r}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _autodetect_provider(ctx: click.Context) -> str | None:
    """Read KEY_PROVIDER from the installed service's .env if any.

    Returns one of ``env``/``file``/``keychain`` if a clean
    auto-detect is possible, otherwise None (we don't auto-pick
    1password — see the module docstring on why)."""
    try:
        install_dir = config.require_installed(ctx.obj.get("install_dir"))
    except NotInstalledError:
        return None
    try:
        env = config.read_env_file(install_dir / ".env")
    except Exception:
        return None
    provider = env.get("KEY_PROVIDER", "").strip().lower()
    if provider in ("env", "file", "keychain"):
        return provider
    return None


def _write_file_backend(path: str, hex_key: str, address: str) -> None:
    p = Path(path)
    if p.exists():
        raise SkrCryptoError(
            f"Refusing to overwrite existing file {p}. Either remove it "
            f"first, or pass a different --path."
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write through a fd opened with mode 0600 atomically — never
    # exists with looser bits even for a moment.
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (hex_key + "\n").encode("ascii"))
    finally:
        os.close(fd)
    # Defensive: re-stat to confirm.
    mode = stat.S_IMODE(p.stat().st_mode)
    output.success(f"wrote {p} (mode 0{mode:o})")
    output.info(f"address: [bold]{address}[/bold]")
    output.info(
        f"Set [italic]PRIVATE_KEY_FILE={p}[/italic] in the service's .env "
        f"and run [italic]skr-crypto restart[/italic]."
    )


def _write_keychain_backend(
    service_name: str, account: str, hex_key: str, address: str,
) -> None:
    if not shutil.which("security"):
        raise SkrCryptoError(
            "macOS `security` tool not on PATH — keychain backend only "
            "works on darwin."
        )
    # `-U` updates an existing item in place if one already exists, so
    # rotating keys via this command "just works".
    result = subprocess.run(
        ["security", "add-generic-password",
         "-U",
         "-s", service_name,
         "-a", account,
         "-w", hex_key],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SkrCryptoError(
            f"security add-generic-password failed: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    output.success(
        f"stored in macOS Keychain (service={service_name} "
        f"account={account})"
    )
    output.info(f"address: [bold]{address}[/bold]")
    output.info(
        "Set [italic]KEY_PROVIDER=keychain[/italic] in the service's .env "
        "and matching [italic]KEYCHAIN_SERVICE/KEYCHAIN_ACCOUNT[/italic]."
    )
