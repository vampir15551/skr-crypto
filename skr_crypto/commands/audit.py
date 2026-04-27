"""``skr-crypto audit`` — browse the durable audit log.

Reads ``data/audit.log`` (the file pointed at by ``AUDIT_LOG_FILE``)
locally, parses each JSON line, optionally filters and pretty-prints.
No HTTP round trip — purely local file I/O. Safe even when the service
is down.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import click

from skr_crypto import config, output

# Filter shorthand: --today / --since / --event / --to / --limit.

_MSK = timezone(timedelta(hours=3))


@click.command("audit")
@click.option("--today", is_flag=True,
              help="Records since 00:00 MSK today (UTC+3).")
@click.option("--since", "since",
              help="ISO timestamp lower-bound (e.g. 2026-04-25T00:00:00).")
@click.option("--event", "event_filter",
              help="Filter by event (SEND_SUCCESS / SEND_FAILED / "
                   "SEND_REJECTED / SEND_DUPLICATE / STARTUP_CHECK).")
@click.option("--to", "to_addr_filter",
              help="Filter by recipient TRON address.")
@click.option("--limit", default=50, show_default=True,
              help="Maximum number of records to show.")
@click.option("--json", "as_json", is_flag=True,
              help="Emit raw JSON records to stdout (one per line).")
@click.pass_context
def cmd(
    ctx: click.Context,
    today: bool,
    since: str | None,
    event_filter: str | None,
    to_addr_filter: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """Print recent audit records (most recent first)."""
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    env = config.read_env_file(install_dir / ".env")
    paths = config.ServicePaths.from_install_dir(install_dir, env)

    if not paths.audit_log.exists():
        output.warn(
            f"No audit log at {paths.audit_log}. Either AUDIT_LOG_FILE is "
            f"unset, or no /send has happened yet (it's lazy-created)."
        )
        return

    lower_bound = _resolve_lower_bound(today, since)

    records = list(_iter_records(
        paths.audit_log,
        lower_bound=lower_bound,
        event=event_filter,
        to_addr=to_addr_filter,
    ))
    # Most recent first.
    records.reverse()
    records = records[:limit]

    if not records:
        output.info("No matching records.")
        return

    if as_json:
        # Bypass Rich (which line-wraps). One record per stdout line,
        # deterministic key order — pipeable into `jq -c` directly.
        import sys
        for r in records:
            sys.stdout.write(json.dumps(r, sort_keys=True) + "\n")
        sys.stdout.flush()
        return

    table = output.make_table(
        "ts (UTC)", "event", "to", "amount", "txid", "result",
        title=f"audit ({len(records)} record{'s' if len(records) != 1 else ''})",
    )
    for r in records:
        table.add_row(
            (r.get("timestamp", "") or "")[:19],
            _colour_event(r.get("event", "")),
            (r.get("to_address", "") or "")[:12] + ("…" if r.get("to_address") else ""),
            str(r.get("amount", "")),
            (r.get("txid", "") or "")[:12] + ("…" if r.get("txid") else ""),
            str(r.get("result", "")),
        )
    output.render_table(table)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve_lower_bound(today: bool, since: str | None) -> datetime | None:
    if today:
        now = datetime.now(_MSK)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(UTC)
    if since:
        # Permissive parse: accept date-only ("2026-04-25") and full ISO.
        try:
            dt = datetime.fromisoformat(since)
        except ValueError:
            try:
                dt = datetime.strptime(since, "%Y-%m-%d")
            except ValueError:
                raise click.BadParameter(
                    f"Could not parse --since={since!r}. Use ISO format "
                    f"(2026-04-25 or 2026-04-25T12:00:00)."
                ) from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    return None


def _iter_records(
    path: Path,
    *,
    lower_bound: datetime | None,
    event: str | None,
    to_addr: str | None,
):
    """Stream records from an append-only audit file. Skips malformed
    lines silently — they're not actionable from a CLI."""
    with open(path, encoding="utf-8") as fp:
        for raw in fp:
            raw = raw.strip()
            if not raw:
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if "event" not in r:
                # The ``audit_init`` marker row — skip.
                continue
            if event and r.get("event") != event:
                continue
            if to_addr and r.get("to_address") != to_addr:
                continue
            if lower_bound is not None:
                ts = _parse_iso(r.get("timestamp", ""))
                if ts is None or ts < lower_bound:
                    continue
            yield r


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _colour_event(event: str) -> str:
    return {
        "SEND_SUCCESS":   f"[green]{event}[/green]",
        "SEND_FAILED":    f"[red]{event}[/red]",
        "SEND_REJECTED":  f"[yellow]{event}[/yellow]",
        "SEND_DUPLICATE": f"[blue]{event}[/blue]",
        "STARTUP_CHECK":  f"[magenta]{event}[/magenta]",
    }.get(event, event)
