"""Terminal output helpers. Thin wrappers around Rich to keep the rest
of the codebase free of ``rich.console`` imports.

Two principles:

  1. Every command's *primary* output (the thing a script would parse)
     goes to **stdout**. Status / progress / decoration goes to **stderr**.
     Pipes stay clean: ``skr-crypto status --json | jq ...`` works
     even with the spinner running.

  2. Colours are auto-detected — Rich respects ``NO_COLOR`` and pipe
     redirection by default. Don't override.
"""
from __future__ import annotations

import json as _json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

# Two Consoles so progress / errors don't pollute stdout when the user
# pipes the command into something else.
_out = Console()
_err = Console(stderr=True)

# `--quiet` at the root suppresses info() / success() but keeps warn() /
# error() — those carry actionable signal.
_QUIET = False


def set_quiet(flag: bool) -> None:
    global _QUIET
    _QUIET = flag


def info(msg: str) -> None:
    """Status update / progress. Goes to stderr. Suppressed by --quiet."""
    if _QUIET:
        return
    _err.print(msg)


def warn(msg: str) -> None:
    """Always shown — actionable warning."""
    _err.print(f"[yellow]warning:[/yellow] {msg}")


def error(msg: str) -> None:
    """Always shown — actionable error."""
    _err.print(f"[red]error:[/red] {msg}")


def success(msg: str) -> None:
    """Operational success line. Suppressed by --quiet."""
    if _QUIET:
        return
    _err.print(f"[green]✓[/green] {msg}")


def print_value(value: Any) -> None:
    """The command's primary output value. Goes to stdout."""
    _out.print(value)


def print_json(payload: Any, *, indent: int = 2) -> None:
    """Emit a JSON payload to stdout. Always machine-friendly: no
    Rich pretty-printing, deterministic key order, no trailing
    whitespace. Designed for ``... | jq``."""
    sys.stdout.write(_json.dumps(payload, indent=indent, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()


def make_table(*headers: str, title: str | None = None) -> Table:
    """Two-line wrapper to centralise default styling."""
    t = Table(title=title, show_lines=False, header_style="bold")
    for h in headers:
        t.add_column(h)
    return t


def render_table(table: Table) -> None:
    """Render a table to stdout (so it can be piped, e.g. into less)."""
    _out.print(table)


def confirm(prompt: str, *, default: bool = False) -> bool:
    """Y/N confirmation. Used for destructive ops (restore, force-install)."""
    suffix = " [Y/n]" if default else " [y/N]"
    try:
        ans = input(f"{prompt}{suffix} ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def ask(prompt: str, *, default: str | None = None, secret: bool = False) -> str:
    """Free-text prompt. Empty input returns ``default``. ``secret=True``
    suppresses echo (used for API keys / token paste).

    On EOF (e.g. piped script with no input) returns ``default``; if
    no default is set and EOF arrives, returns "" so callers can detect
    the non-interactive case via ``ask(..., default="") == ""``.
    """
    suffix = f" [{default}]" if default else ""
    try:
        if secret:
            import getpass
            value = getpass.getpass(f"{prompt}{suffix}: ")
        else:
            value = input(f"{prompt}{suffix}: ")
    except EOFError:
        return default or ""
    value = value.strip()
    return value if value else (default or "")


def ask_choice(prompt: str, choices: tuple[str, ...], default: str) -> str:
    """Constrained prompt: keep asking until the answer is in ``choices``.

    Renders as ``prompt (a/b/c)`` with the default underlined via
    asterisks (``*default*``) since ``input()`` doesn't render Rich
    markup.
    """
    while True:
        rendered = "/".join(
            f"*{c}*" if c == default else c for c in choices
        )
        chosen = ask(f"{prompt} ({rendered})", default=default).lower()
        if chosen in choices:
            return chosen
        warn(f"must be one of: {', '.join(choices)}")
