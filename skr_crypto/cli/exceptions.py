"""Typed exceptions for the CLI.

Each maps to a stable exit code so scripts wrapping ``skr-crypto`` can
branch on outcome without parsing stderr. We extend ``click.ClickException``
so click's standard machinery (used both by the installed entry point
and ``click.testing.CliRunner``) reliably honours the exit code.
"""
from __future__ import annotations

import click


class SkrCryptoError(click.ClickException):
    """Base exception. Maps to exit code 1.

    Override ``exit_code`` in subclasses for finer-grained signalling.
    Override ``show()`` to customise rendering — by default we keep
    click's red-prefixed stderr line, which is fine.
    """

    exit_code: int = 1

    def show(self, file=None) -> None:  # type: ignore[override]
        click.echo(
            click.style(f"error: {self.message}", fg="red"),
            err=True,
        )


class NotInstalledError(SkrCryptoError):
    """No service installation found at the resolved install dir.

    Maps to exit code 2 — distinguishable from a generic failure so a
    deploy script can branch ('not installed yet → run install').
    """

    exit_code = 2


class AlreadyInstalledError(SkrCryptoError):
    """An install dir already exists. ``install --force`` overrides."""

    exit_code = 3


class ServiceUnreachableError(SkrCryptoError):
    """The HTTP API does not respond. Maps to exit code 4."""

    exit_code = 4


class AuthError(SkrCryptoError):
    """API rejected our X-API-Key. Maps to exit code 5."""

    exit_code = 5


class BadResponseError(SkrCryptoError):
    """API responded with a non-2xx or unparseable body."""

    exit_code = 6


class GitOperationError(SkrCryptoError):
    """A git clone/fetch/checkout failed."""

    exit_code = 7


class VenvError(SkrCryptoError):
    """venv creation or pip install failed."""

    exit_code = 8


class ConfigError(SkrCryptoError):
    """The service's .env is missing or malformed."""

    exit_code = 9


# ---------------------------------------------------------------------------
# Risk-check exit codes — used by `skr-crypto risk`.
# ---------------------------------------------------------------------------
# `risk` is informational, not an error per se, but operators want a
# stable exit-code-per-level so they can chain it in shell scripts:
#
#     skr-crypto risk $ADDR && curl ... /api/v1/send ...
#
# 0  = LOW (proceed)
# 10 = MEDIUM (warn — at least one WARN/FAIL fired but nothing high-severity)
# 11 = HIGH  (refuse — Tether-blacklisted, contract destination, burn pattern)
# 12 = INVALID (address itself doesn't validate)
RISK_EXIT_LOW = 0
RISK_EXIT_MEDIUM = 10
RISK_EXIT_HIGH = 11
RISK_EXIT_INVALID = 12
