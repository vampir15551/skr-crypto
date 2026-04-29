"""``skr-crypto config show / edit / validate``.

This is a click *group* (subcommands), unlike the rest which are flat
commands. Three sub-actions:

  - ``config show``     — print .env with sensitive values masked
  - ``config edit``     — open .env in $EDITOR (or $VISUAL, fallback nano)
  - ``config validate`` — invoke the service's own validate_config()
                          inside its venv, surfacing any errors
"""
from __future__ import annotations

import os
import subprocess

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.exceptions import SkrCryptoError


@click.group("config")
def config_group() -> None:
    """Inspect or edit the service's configuration (.env)."""


@config_group.command("show")
@click.option("--unsafe-show-secrets", is_flag=True,
              help="Show secrets in plain text. Don't use this on a "
                   "shared screen.")
@click.pass_context
def show(ctx: click.Context, unsafe_show_secrets: bool) -> None:
    """Print the .env with secrets masked.

    Sensitive keys (AUTH_TOKEN, TRONGRID_API_KEY, ...) show only the
    first 4 chars + '…'. The full file is still on disk at .env if
    you really need it.
    """
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    env = config.read_env_file(install_dir / ".env")

    table = output.make_table("Key", "Value", title="config")
    for k, v in env.items():
        if k in config.SENSITIVE_KEYS and not unsafe_show_secrets:
            shown = f"{v[:4]}…" if v else "(empty)"
            table.add_row(f"[bold]{k}[/bold]", f"[yellow]{shown}[/yellow]")
        else:
            table.add_row(k, v or "(empty)")
    output.render_table(table)


@config_group.command("edit")
@click.pass_context
def edit(ctx: click.Context) -> None:
    """Open .env in $EDITOR (fallbacks: $VISUAL, nano, vi)."""
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    env_path = install_dir / ".env"
    editor = (
        os.environ.get("EDITOR")
        or os.environ.get("VISUAL")
        or _first_available(["nano", "vim", "vi"])
    )
    if not editor:
        raise SkrCryptoError(
            "No editor found. Set $EDITOR or install nano/vim/vi."
        )
    output.info(f"opening {env_path} in {editor}")
    rc = subprocess.call([editor, str(env_path)])
    if rc != 0:
        raise SkrCryptoError(f"editor exited with status {rc}")
    output.success("saved (restart the service for changes to take effect)")


@config_group.command("validate")
@click.pass_context
def validate(ctx: click.Context) -> None:
    """Run skr_crypto.server.config.validate_config() against the current .env.

    Catches the common mistakes (AUTH_TOKEN unset, MIN_TRX_RESERVE too
    low, KEY_PROVIDER misspelled, etc.) without having to start the
    service. validate_config() exits 1 on any error and writes them to
    stderr, which we surface verbatim.
    """
    import sys
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    result = subprocess.run(
        [sys.executable, "-c",
         "from skr_crypto.server.config import validate_config; validate_config()"],
        cwd=str(install_dir),
        check=False,
    )
    if result.returncode != 0:
        raise SkrCryptoError("config validation failed (see stderr above)")
    output.success("config is valid")


def _first_available(candidates: list[str]) -> str | None:
    import shutil
    for c in candidates:
        if shutil.which(c):
            return c
    return None
