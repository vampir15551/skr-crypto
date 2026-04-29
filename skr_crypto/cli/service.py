"""Lifecycle abstraction over the underlying service.

The service can be running under one of three regimes (in order of
detection priority):

  1. **systemd** — a unit file at ``/etc/systemd/system/payouts.service``
     (or any name matching ``payouts*.service``).
  2. **docker compose** — a ``docker-compose.yml`` next to the install
     dir's contents and ``docker compose ps`` showing a running service.
  3. **direct** — neither of the above; the user runs ``./run.sh`` by
     hand. We can't start/stop/restart in this mode (no supervision),
     but we can show status and tail the log file.

Detection is heuristic. ``--via systemd|compose|direct`` on each
command overrides it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Regime(Enum):
    SYSTEMD = "systemd"
    COMPOSE = "compose"
    DIRECT = "direct"


@dataclass(frozen=True)
class ServiceContext:
    regime: Regime
    install_dir: Path
    # systemd: unit name (e.g. "payouts.service")
    # compose: project name from compose file
    # direct: None
    handle: str | None


def detect(install_dir: Path, *, override: str | None = None) -> ServiceContext:
    """Return the most likely regime for this install."""
    if override:
        try:
            forced = Regime(override)
        except ValueError:
            raise ValueError(
                f"--via {override!r} is not one of: systemd, compose, direct"
            ) from None
        return ServiceContext(forced, install_dir, _handle_for(forced, install_dir))

    if _systemd_unit_name(install_dir):
        return ServiceContext(
            Regime.SYSTEMD, install_dir,
            _systemd_unit_name(install_dir),
        )
    if (install_dir / "docker-compose.yml").exists() and shutil.which("docker"):
        return ServiceContext(
            Regime.COMPOSE, install_dir, install_dir.name,
        )
    return ServiceContext(Regime.DIRECT, install_dir, None)


def _systemd_unit_name(install_dir: Path) -> str | None:
    """Return the systemd unit name if a unit file points at this install
    dir. We look for the conventional name first, then any unit whose
    ``WorkingDirectory=`` matches.
    """
    if os.uname().sysname != "Linux":
        return None
    if not shutil.which("systemctl"):
        return None
    # Canonical name first, legacy name kept for installs that came from
    # the pre-1.0.0 flow.
    candidates = [
        Path("/etc/systemd/system/skr-crypto.service"),
        Path("/etc/systemd/system/payouts.service"),
    ]
    for c in candidates:
        if c.exists() and str(install_dir) in c.read_text(errors="ignore"):
            return c.name
    return None


def _handle_for(regime: Regime, install_dir: Path) -> str | None:
    if regime is Regime.SYSTEMD:
        return _systemd_unit_name(install_dir) or "skr-crypto.service"
    if regime is Regime.COMPOSE:
        return install_dir.name
    return None


# ---------------------------------------------------------------------------
# Lifecycle ops — return (returncode, stdout, stderr) so callers can
# format the error in their own preferred style.
# ---------------------------------------------------------------------------


def start(ctx: ServiceContext) -> subprocess.CompletedProcess[str]:
    if ctx.regime is Regime.SYSTEMD:
        return _run(["sudo", "systemctl", "start", ctx.handle or "skr-crypto.service"])
    if ctx.regime is Regime.COMPOSE:
        return _run(["docker", "compose", "up", "-d"], cwd=ctx.install_dir)
    raise NotImplementedError(
        "Cannot start a 'direct' install (no supervisor). "
        "Run `skr-crypto-server` in another terminal, "
        "or set up systemd / docker compose using deploy/."
    )


def stop(ctx: ServiceContext) -> subprocess.CompletedProcess[str]:
    if ctx.regime is Regime.SYSTEMD:
        return _run(["sudo", "systemctl", "stop", ctx.handle or "skr-crypto.service"])
    if ctx.regime is Regime.COMPOSE:
        return _run(["docker", "compose", "down"], cwd=ctx.install_dir)
    raise NotImplementedError(
        "Cannot stop a 'direct' install — Ctrl-C the running `skr-crypto-server`."
    )


def restart(ctx: ServiceContext) -> subprocess.CompletedProcess[str]:
    if ctx.regime is Regime.SYSTEMD:
        return _run(["sudo", "systemctl", "restart", ctx.handle or "skr-crypto.service"])
    if ctx.regime is Regime.COMPOSE:
        return _run(
            ["docker", "compose", "up", "-d", "--force-recreate"],
            cwd=ctx.install_dir,
        )
    raise NotImplementedError(
        "Cannot restart a 'direct' install — Ctrl-C and re-run `skr-crypto-server`."
    )


def logs_command(ctx: ServiceContext, *, follow: bool, lines: int) -> list[str]:
    """Return the argv for the appropriate logs command. The caller
    runs it themselves so it can stream stdout/stderr live."""
    if ctx.regime is Regime.SYSTEMD:
        argv = ["journalctl", "-u", ctx.handle or "skr-crypto.service",
                "-n", str(lines), "--no-pager"]
        if follow:
            argv.append("-f")
        return argv
    if ctx.regime is Regime.COMPOSE:
        argv = ["docker", "compose", "logs",
                "--tail", str(lines)]
        if follow:
            argv.append("-f")
        return argv
    # direct: just tail the audit file if present, otherwise nothing
    return ["tail", "-n", str(lines), "-f" if follow else "", "data/audit.log"]


def _run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, check=False,
    )
