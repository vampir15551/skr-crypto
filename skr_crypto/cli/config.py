"""Resolve install paths and read the service's ``.env`` (read-only).

The CLI itself has minimal config of its own — at most an install path.
The *service* has the meaningful config (AUTH_TOKEN, paths, network);
we read it on demand and never cache it across commands so the user
can edit ``.env`` and the next command sees fresh values.

Resolution order for the install dir:

  1. ``--dir`` flag on the invoked command
  2. ``SKR_CRYPTO_HOME`` env var
  3. ``~/.skr-crypto`` (default)

Inside the install dir we expect a layout matching the service:

    <install_dir>/
        venv/
        .env
        run.sh
        app/
        data/
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from skr_crypto.cli.exceptions import ConfigError, NotInstalledError

DEFAULT_INSTALL_DIR = Path.home() / ".skr-crypto"


def install_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the install dir without asserting it exists."""
    if override:
        return Path(override).expanduser().resolve()
    env = os.environ.get("SKR_CRYPTO_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_INSTALL_DIR.resolve()


def require_installed(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the install dir and assert ``.env`` is present.

    Since v1.0.0 the service code is part of this Python package, so
    we don't look for ``app/`` anymore — only the runtime state file
    (``.env``). Raises NotInstalledError otherwise. Used by every
    command except ``install`` and ``doctor`` (the latter inspects
    state regardless).
    """
    p = install_dir(override)
    if not (p / ".env").exists():
        raise NotInstalledError(
            f"No service install found at {p}. Run `skr-crypto install` first."
        )
    return p


_ENV_LINE = re.compile(r"^([A-Z_][A-Z0-9_]*)\s*=(.*)$")


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv file. Lightweight — doesn't honour ``export`` or
    multiline values. Matches the subset python-dotenv uses for this
    project's ``.env``."""
    out: dict[str, str] = {}
    if not path.exists():
        raise ConfigError(f".env not found at {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        out[key] = val
    return out


def write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write a ``.env``. The file is chmod 600 — it will contain
    AUTH_TOKEN at minimum, and possibly TRONGRID_API_KEY."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in values.items() if v != ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


# Keys that must NEVER be displayed by `skr-crypto config show`. The
# command masks these values; the user can still ``cat .env`` if they
# want to see them, but we don't put them on screen by accident.
SENSITIVE_KEYS = frozenset({
    "AUTH_TOKEN",
    "TRONGRID_API_KEY",
    "PRIVATE_KEY_HEX",
})


@dataclass(frozen=True)
class ServicePaths:
    """Resolved paths for the installed service. Constructed by callers
    that need more than just the .env values."""

    root: Path
    env_file: Path
    venv_python: Path
    audit_log: Path
    idempotency_db: Path
    data_dir: Path

    @classmethod
    def from_install_dir(cls, root: Path, env: dict[str, str]) -> ServicePaths:
        # Service uses paths relative to its own root (run.sh always cd's
        # there). We honour the same convention.
        def _resolve(rel_or_abs: str, default: str) -> Path:
            v = rel_or_abs or default
            p = Path(v)
            return p if p.is_absolute() else (root / p)

        return cls(
            root=root,
            env_file=root / ".env",
            venv_python=root / "venv" / "bin" / "python",
            audit_log=_resolve(env.get("AUDIT_LOG_FILE", ""), "data/audit.log"),
            idempotency_db=_resolve(env.get("IDEMPOTENCY_DB_PATH", ""), "data/idempotency.db"),
            data_dir=root / "data",
        )
