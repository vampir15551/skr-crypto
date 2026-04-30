"""Single source of truth for the package version.

Read by:
  - ``pyproject.toml`` (dynamic version)
  - the ``skr-crypto version`` command
  - GitHub Actions release workflow (asserts the tag matches)

Bump together with a CHANGELOG entry under ``[Unreleased]`` → version
heading. See ``RELEASE.md``.
"""
from __future__ import annotations

__version__: str = "1.5.0"
