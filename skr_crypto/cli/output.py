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


def info(msg: str) -> None:
    """Status update / progress. Goes to stderr."""
    _err.print(msg)


def warn(msg: str) -> None:
    _err.print(f"[yellow]warning:[/yellow] {msg}")


def error(msg: str) -> None:
    _err.print(f"[red]error:[/red] {msg}")


def success(msg: str) -> None:
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
