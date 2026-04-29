"""Build/version metadata captured at process start.

Resolution order for the git sha:

  1. The ``GIT_SHA`` env var if present — the canonical override.
     Useful for Docker images built from a tarball, where there's no
     ``.git`` directory at runtime; the build step bakes the sha in.
  2. ``git rev-parse --short=12 HEAD`` from the working tree.
  3. The literal string ``"unknown"`` — never raise, this is just a
     diagnostic.

Captured once at import time so ``/api/v1/version`` is free of
subprocess overhead and cannot drift mid-process.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time

log = logging.getLogger("payouts")

START_TIME: float = time.time()


def _resolve_git_sha() -> str:
    env_sha = os.environ.get("GIT_SHA", "").strip()
    if env_sha:
        return env_sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception as exc:
        log.debug("git rev-parse failed: %s", exc)
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return (result.stdout or "").strip() or "unknown"


GIT_SHA: str = _resolve_git_sha()
