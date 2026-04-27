"""``skr-crypto install`` — clone the service from git, set it up.

What we do:

  1. ``git clone --depth 1 --branch <ref>`` into ``--dir`` (default
     ``~/.skr-crypto``).
  2. ``python3 -m venv venv`` inside it.
  3. ``pip install -r requirements.txt`` (or ``--require-hashes -r
     requirements.lock`` if present).
  4. Generate a high-entropy ``AUTH_TOKEN``.
  5. Write a minimal ``.env`` with safe defaults that *does not* yet
     contain a private key — the user picks a key provider and supplies
     the secret afterwards.
  6. ``mkdir -p data`` (audit + idempotency DB live there).
  7. Print a checklist of next steps.

This is a non-interactive, reproducible install. ``--interactive``
flips it into a wizard if the user wants to be prompted for every
field — that calls into the service's own ``scripts/setup.py``.
"""
from __future__ import annotations

import secrets
from pathlib import Path

import click

from skr_crypto import config, output
from skr_crypto.exceptions import AlreadyInstalledError
from skr_crypto.installer import create_venv, git_clone, pip_install

DEFAULT_GIT_URL = "https://github.com/vampir15551/skr_crypto-payouts.git"
DEFAULT_REF = "main"

# Defaults written into .env on a fresh install. The operator can edit
# afterwards via `skr-crypto config edit`.
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
@click.option("--git-url", default=DEFAULT_GIT_URL, show_default=True,
              help="Git URL to clone the service from.")
@click.option("--ref", default=DEFAULT_REF, show_default=True,
              help="Branch / tag / commit to check out.")
@click.option("--from-path", "from_path",
              type=click.Path(exists=True, file_okay=False, resolve_path=True),
              help="Install from a local path instead of cloning. Useful for "
                   "dev: --from-path ../Payouts copies that working tree.")
@click.option("--force", is_flag=True,
              help="Wipe an existing install dir before installing.")
@click.option("--interactive", is_flag=True,
              help="Run the service's own setup.py wizard for every field.")
@click.option("--no-deps", is_flag=True,
              help="Skip pip install (for tests, or when offline).")
@click.pass_context
def cmd(
    ctx: click.Context,
    git_url: str,
    ref: str,
    from_path: str | None,
    force: bool,
    interactive: bool,
    no_deps: bool,
) -> None:
    """Install the service from git into the configured dir."""
    install_dir = config.install_dir(ctx.obj.get("install_dir"))

    if install_dir.exists() and any(install_dir.iterdir()):
        if not force:
            raise AlreadyInstalledError(
                f"{install_dir} is not empty. Use --force to wipe and reinstall, "
                f"or --dir to install elsewhere."
            )
        output.warn(f"--force: removing {install_dir}")
        _rmtree(install_dir)

    output.info(f"Installing into [bold]{install_dir}[/bold]")

    # ── 1. Materialise the working tree ─────────────────────────────────
    if from_path:
        output.info(f"Copying from local path: {from_path}")
        _copytree(Path(from_path), install_dir)
    else:
        output.info(f"Cloning {git_url}@{ref}")
        git_clone(git_url, ref, install_dir)
        output.success(f"cloned to {install_dir}")

    # ── 2. venv + deps ──────────────────────────────────────────────────
    if not no_deps:
        output.info("Creating virtualenv (venv)")
        create_venv(install_dir)
        output.info("Installing dependencies (this may take a minute)")
        pip_install(install_dir / "venv" / "bin" / "python", install_dir)
        output.success("dependencies installed")

    # ── 3. Generate AUTH_TOKEN ──────────────────────────────────────────
    auth_token = secrets.token_urlsafe(32)
    output.success("generated fresh AUTH_TOKEN")

    # ── 4. Write .env (or run interactive wizard) ───────────────────────
    if interactive:
        output.info("Launching the service's interactive setup wizard")
        _run_service_wizard(install_dir)
    else:
        env_values = dict(DEFAULT_ENV)
        env_values["AUTH_TOKEN"] = auth_token
        env_path = install_dir / ".env"
        config.write_env_file(env_path, env_values)
        output.success(f"wrote {env_path} (mode 0600)")

    # ── 5. data/ ────────────────────────────────────────────────────────
    (install_dir / "data").mkdir(exist_ok=True)
    output.success(f"created {install_dir / 'data'}")

    # ── 6. Next-steps checklist ─────────────────────────────────────────
    output.info("")
    output.info("[bold]Next steps:[/bold]")
    output.info(
        "  1. Set the private key. With KEY_PROVIDER=env (default), "
        "export it before starting:"
    )
    output.info("       export PRIVATE_KEY_HEX='<32-byte-hex>'")
    output.info(
        "     Or switch to a different provider: "
        "[italic]skr-crypto config edit[/italic]"
    )
    output.info("  2. Set TRONGRID_API_KEY in .env (highly recommended).")
    output.info("  3. Start: [italic]skr-crypto start[/italic]")
    output.info("  4. Verify: [italic]skr-crypto status[/italic]")
    output.info("")
    output.info(f"AUTH_TOKEN (also in .env): [yellow]{auth_token}[/yellow]")
    output.info("Save this — your API clients need it as the X-API-Key header.")


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path)


def _copytree(src: Path, dst: Path) -> None:
    """Copy ``src`` → ``dst`` skipping virtualenvs / caches / state.

    Mirrors what a clean ``git archive`` would produce, without needing
    git. Used by ``--from-path`` for local-dev installs.
    """
    import shutil
    skip = {"venv", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
            "data", "node_modules", ".git", ".idea", ".vscode"}

    def ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n in skip or n.endswith(".pyc")]

    shutil.copytree(src, dst, ignore=ignore)


def _run_service_wizard(install_dir: Path) -> None:
    """Hand off to the service's own setup.py for full interactive setup.

    The service's wizard handles the same fields the non-interactive
    path does, plus per-key-provider questions. We just invoke it inside
    the venv so it sees its own deps.
    """
    import subprocess
    setup_py = install_dir / "scripts" / "setup.py"
    if not setup_py.exists():
        output.warn(
            f"{setup_py} not found — falling back to default .env template."
        )
        return
    subprocess.run(
        [str(install_dir / "venv" / "bin" / "python"), str(setup_py)],
        cwd=str(install_dir),
        check=True,
    )
