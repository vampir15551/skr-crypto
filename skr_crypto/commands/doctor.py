"""``skr-crypto doctor`` — environment diagnostics, ``brew doctor``-style.

Walks a list of self-contained checks. Each returns ``(status, message)``
where status is ``ok | warn | fail``. The CLI exits 0 if all are
``ok|warn``, 1 if any ``fail`` — so CI / cron can branch on it.

Adding a check: append a function to ``CHECKS`` returning
``(Status, str)``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from enum import Enum
from pathlib import Path

import click

from skr_crypto import config, output
from skr_crypto.exceptions import NotInstalledError


class Status(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


# A check is (name, callable returning (Status, message)).
Check = tuple[str, "callable"]


def _check_python() -> tuple[Status, str]:
    import sys
    v = sys.version_info
    if v < (3, 11):
        return Status.FAIL, f"Python {v.major}.{v.minor} — service needs ≥3.11"
    return Status.OK, f"Python {v.major}.{v.minor}.{v.micro}"


def _check_git() -> tuple[Status, str]:
    if not shutil.which("git"):
        return Status.FAIL, "git not on PATH"
    try:
        out = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, check=True,
        )
        return Status.OK, out.stdout.strip()
    except subprocess.CalledProcessError as exc:
        return Status.FAIL, f"git --version failed: {exc}"


def _check_disk_space(install_dir: Path) -> tuple[Status, str]:
    target = install_dir if install_dir.exists() else install_dir.parent
    while not target.exists():
        target = target.parent
    free = shutil.disk_usage(target).free
    if free < 200 * 1024 * 1024:
        return Status.FAIL, f"only {free // 1024 // 1024} MB free at {target}"
    if free < 1024 * 1024 * 1024:
        return Status.WARN, f"only {free // 1024 // 1024} MB free at {target}"
    return Status.OK, f"{free // 1024 // 1024} MB free at {target}"


def _check_install(install_dir: Path) -> tuple[Status, str]:
    if not (install_dir / ".env").exists():
        return Status.WARN, f"no install at {install_dir} (run `skr-crypto install`)"
    return Status.OK, str(install_dir)


def _check_env_secrets(install_dir: Path) -> tuple[Status, str]:
    env_path = install_dir / ".env"
    if not env_path.exists():
        return Status.WARN, ".env not present"
    env = config.read_env_file(env_path)
    if not env.get("AUTH_TOKEN"):
        return Status.FAIL, "AUTH_TOKEN unset"
    if len(env["AUTH_TOKEN"]) < 24:
        return Status.WARN, "AUTH_TOKEN looks short (< 24 chars)"
    if not env.get("TRONGRID_API_KEY"):
        return Status.WARN, "TRONGRID_API_KEY unset (rate-limit risk)"
    return Status.OK, "auth + api key set"


def _check_env_perms(install_dir: Path) -> tuple[Status, str]:
    env_path = install_dir / ".env"
    if not env_path.exists():
        return Status.WARN, ".env not present"
    mode = env_path.stat().st_mode & 0o777
    if mode & 0o077:
        return Status.FAIL, f"{env_path} mode {mode:o} — group/other can read it"
    return Status.OK, f".env mode {mode:o}"


def _check_key_provider(install_dir: Path) -> tuple[Status, str]:
    env_path = install_dir / ".env"
    if not env_path.exists():
        return Status.WARN, ".env not present"
    env = config.read_env_file(env_path)
    provider = env.get("KEY_PROVIDER", "1password")
    if provider == "1password":
        if not shutil.which("op"):
            return Status.FAIL, "KEY_PROVIDER=1password but `op` CLI not on PATH"
        return Status.OK, "1password (op CLI present)"
    if provider == "env":
        if not os.environ.get("PRIVATE_KEY_HEX"):
            return Status.WARN, "KEY_PROVIDER=env but PRIVATE_KEY_HEX is not exported"
        return Status.OK, "env (PRIVATE_KEY_HEX present)"
    if provider == "file":
        path = env.get("PRIVATE_KEY_FILE", "")
        if not path:
            return Status.FAIL, "KEY_PROVIDER=file but PRIVATE_KEY_FILE unset"
        p = Path(path)
        if not p.exists():
            return Status.FAIL, f"PRIVATE_KEY_FILE not found: {p}"
        mode = p.stat().st_mode & 0o777
        if mode & 0o077:
            return Status.FAIL, f"{p} mode {mode:o} — service will refuse to load"
        return Status.OK, f"file {p} (mode {mode:o})"
    if provider == "keychain":
        if not shutil.which("security"):
            return Status.FAIL, "KEY_PROVIDER=keychain but `security` not on PATH"
        return Status.OK, "keychain (security tool present)"
    return Status.FAIL, f"KEY_PROVIDER={provider!r} is not a known value"


def _check_data_dir(install_dir: Path) -> tuple[Status, str]:
    env_path = install_dir / ".env"
    if not env_path.exists():
        return Status.WARN, ".env not present"
    env = config.read_env_file(env_path)
    paths = config.ServicePaths.from_install_dir(install_dir, env)
    if not paths.data_dir.exists():
        return Status.WARN, f"data dir missing at {paths.data_dir}"
    return Status.OK, f"data dir present ({paths.data_dir})"


def _check_running_as_root() -> tuple[Status, str]:
    if os.geteuid() == 0:
        return Status.WARN, "running as root — not recommended for ops"
    return Status.OK, f"running as uid {os.geteuid()}"


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def cmd(ctx: click.Context, as_json: bool) -> None:
    """Walk environment diagnostics. Fails (exit 1) if any check fails.

    Use this after install + setup, on every host you deploy to, and
    in CI before publishing a release. Output is stable across releases
    so you can grep it.
    """
    try:
        install_dir = config.install_dir(ctx.obj.get("install_dir"))
    except NotInstalledError:
        install_dir = config.DEFAULT_INSTALL_DIR

    checks: list[tuple[str, tuple[Status, str]]] = [
        ("python",        _check_python()),
        ("git",           _check_git()),
        ("disk space",    _check_disk_space(install_dir)),
        ("install",       _check_install(install_dir)),
        ("env perms",     _check_env_perms(install_dir)),
        ("env secrets",   _check_env_secrets(install_dir)),
        ("key provider",  _check_key_provider(install_dir)),
        ("data dir",      _check_data_dir(install_dir)),
        ("not as root",   _check_running_as_root()),
    ]

    if as_json:
        output.print_json([
            {"check": name, "status": status.value, "message": msg}
            for name, (status, msg) in checks
        ])
    else:
        table = output.make_table("Check", "Status", "Detail", title="doctor")
        for name, (status, msg) in checks:
            colour = {"ok": "green", "warn": "yellow", "fail": "red"}[status.value]
            table.add_row(name, f"[{colour}]{status.value.upper()}[/{colour}]", msg)
        output.render_table(table)

    if any(s is Status.FAIL for _, (s, _) in checks):
        ctx.exit(1)
