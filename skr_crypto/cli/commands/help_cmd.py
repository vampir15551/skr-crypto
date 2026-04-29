"""``skr-crypto help`` — pretty list of every command, grouped by area.

Click's own ``--help`` already lists commands, but flat. This grouped
view is the one the README points new users at, and the one the user
explicitly asked for: ``voka-crypto help для всех команд``.
"""
from __future__ import annotations

import click

from skr_crypto.cli import output

# Group → (command, one-line summary). Matches the names registered in
# cli.py. Update both when adding a new command.
GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Lifecycle", [
        ("install", "Bootstrap the install dir (interactive wizard or --yes)"),
        ("update",  "Show the latest release wheel + how to upgrade"),
        ("start",   "Start the installed service (systemd / docker / direct)"),
        ("stop",    "Stop the running service"),
        ("restart", "Restart the service"),
    ]),
    ("Inspect", [
        ("status",  "Running? Healthy? Which version?"),
        ("logs",    "Tail recent logs (follow with -f)"),
        ("balance", "TRX/USDT balances + on-chain resources"),
        ("check",   "Look up a tx hash on-chain (read-only)"),
        ("audit",   "Browse the service's audit log"),
        ("reconcile", "Re-run the startup-check on demand"),
        ("version", "CLI version + service version"),
    ]),
    ("Configuration", [
        ("config show",     "Show config with secrets masked"),
        ("config edit",     "Open .env in $EDITOR"),
        ("config validate", "Run the service's config-validation checks"),
        ("keygen",          "Generate a fresh TRON private key (one-time, shown once)"),
    ]),
    ("Backups", [
        ("backup",  "Snapshot the data dir (audit + idempotency DB)"),
        ("restore", "Restore from a backup archive"),
    ]),
    ("Diagnostics", [
        ("doctor",     "Walk through environment checks (like brew doctor)"),
        ("completion", "Print a shell-completion script (bash / zsh / fish)"),
    ]),
    ("Help", [
        ("help",    "This screen"),
    ]),
]


@click.command("help")
@click.argument("topic", required=False)
@click.pass_context
def cmd(ctx: click.Context, topic: str | None) -> None:
    """Print all commands grouped by area.

    With a TOPIC argument, dispatches to ``<TOPIC> --help`` so
    ``skr-crypto help install`` works the same as
    ``skr-crypto install --help``.
    """
    if topic:
        # Dispatch into the click subcommand's own --help.
        root = ctx.find_root().command
        sub = root.get_command(ctx, topic)
        if sub is None:
            output.error(f"unknown command: {topic}")
            ctx.exit(1)
        click.echo(sub.get_help(ctx))
        return

    output.print_value(
        "[bold]skr-crypto[/bold] — operator CLI for the SKR Crypto fleet.\n"
    )
    for group, items in GROUPS:
        output.print_value(f"[bold cyan]{group}[/bold cyan]")
        for name, desc in items:
            output.print_value(f"  [green]{name:<18}[/green] {desc}")
        output.print_value("")
    output.print_value(
        "Run [italic]skr-crypto <command> --help[/italic] for full options.\n"
        "Money moves only via the service's authenticated HTTP API — "
        "this CLI is intentionally read-only on that path."
    )
