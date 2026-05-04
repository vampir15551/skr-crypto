"""``skr-crypto report ...`` — period summaries from the audit log (1.9.0+).

Two report types:

  - ``report --period YYYY-MM`` (or ``--from / --to``): daily totals
    + USDT volume + counts. Default for "show me April".
  - ``report sanctions-hits``: every SEND_REJECTED whose details
    name the sanctions check, in chronological order.

Output formats:

  - ``--format csv`` (default): UTF-8 CSV to stdout (``-o`` to file).
  - ``--format xlsx``: requires the ``[reports]`` extra
    (``pip install 'skr-crypto[server,reports]'``); ``-o FILE`` is
    required because XLSX is binary.

See ADR 0014.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.exceptions import SkrCryptoError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_audit_log_path(ctx: click.Context) -> str:
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    env = config.read_env_file(install_dir / ".env")
    raw = env.get("AUDIT_LOG_FILE", "")
    if not raw:
        raise SkrCryptoError(
            "AUDIT_LOG_FILE is not set in .env — reporting needs the "
            "durable audit log. Add it (e.g. AUDIT_LOG_FILE=data/audit.log) "
            "and restart the service."
        )
    p = Path(raw)
    if not p.is_absolute():
        p = install_dir / p
    return str(p)


def _resolve_dates(
    period: str | None, from_str: str | None, to_str: str | None,
) -> tuple[date, date]:
    """Resolve --period / --from / --to into a (from, to) date pair."""
    from skr_crypto.server import reporting

    if period:
        if from_str or to_str:
            raise SkrCryptoError("--period is mutually exclusive with --from/--to")
        return reporting.parse_period_yyyymm(period)
    if not from_str and not to_str:
        # Default: last 30 days ending today (UTC).
        today = date.today()
        return today - timedelta(days=29), today
    if not (from_str and to_str):
        raise SkrCryptoError("--from and --to must be provided together")
    try:
        from_date = date.fromisoformat(from_str)
        to_date = date.fromisoformat(to_str)
    except ValueError as exc:
        raise SkrCryptoError(f"invalid date: {exc}") from exc
    return from_date, to_date


def _write_or_emit(
    payload: bytes | str, *, output_path: str | None, is_binary: bool,
) -> None:
    """If output_path is set, write to file; else dump to stdout.

    XLSX (binary) without -o is an error — binary on a TTY mangles
    the terminal. Surfaced with a clear message at the call site.
    """
    if output_path:
        mode = "wb" if is_binary else "w"
        with open(output_path, mode) as fp:
            fp.write(payload)
        output.success(f"wrote {output_path}")
        return
    if is_binary:
        sys.stdout.buffer.write(payload)
    else:
        sys.stdout.write(payload if isinstance(payload, str) else payload.decode())


# ---------------------------------------------------------------------------
# `report` (period summary, default subcommand)
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.option("--period", default=None,
              help="UTC month, YYYY-MM. Mutually exclusive with --from/--to.")
@click.option("--from", "from_str", default=None,
              help="Inclusive UTC start date YYYY-MM-DD.")
@click.option("--to", "to_str", default=None,
              help="Inclusive UTC end date YYYY-MM-DD. Default: last 30 days.")
@click.option("--format", "fmt", default="csv",
              type=click.Choice(["csv", "xlsx"], case_sensitive=False),
              show_default=True,
              help="Output format. xlsx requires the [reports] extra.")
@click.option("-o", "--output", "output_path", default=None,
              help="Write to FILE instead of stdout. Required for --format xlsx.")
@click.pass_context
def cmd(
    ctx: click.Context, period: str | None, from_str: str | None,
    to_str: str | None, fmt: str, output_path: str | None,
) -> None:
    """Period summary report (counts + volumes per UTC day).

    Use [italic]skr-crypto report sanctions-hits[/italic] for the
    sanctions-only report.
    """
    if ctx.invoked_subcommand is not None:
        # A subcommand was invoked; let it handle the work.
        return

    from skr_crypto.server import reporting

    audit_path = _resolve_audit_log_path(ctx)
    from_date, to_date = _resolve_dates(period, from_str, to_str)
    try:
        reporting.validate_range(from_date, to_date)
    except ValueError as exc:
        raise SkrCryptoError(str(exc)) from exc

    summary = reporting.aggregate_period(
        audit_path, from_date=from_date, to_date=to_date,
    )

    if fmt == "xlsx":
        if not output_path:
            raise SkrCryptoError(
                "--format xlsx requires -o FILE (binary on TTY mangles output)"
            )
        try:
            data = reporting.render_period_xlsx(summary)
        except reporting.XlsxExtraMissing as exc:
            raise SkrCryptoError(str(exc)) from exc
        _write_or_emit(data, output_path=output_path, is_binary=True)
        return

    csv_text = reporting.render_period_csv(summary)
    _write_or_emit(csv_text, output_path=output_path, is_binary=False)


# ---------------------------------------------------------------------------
# `report sanctions-hits`
# ---------------------------------------------------------------------------


@cmd.command("sanctions-hits")
@click.option("--from", "from_str", default=None,
              help="Inclusive UTC start date YYYY-MM-DD.")
@click.option("--to", "to_str", default=None,
              help="Inclusive UTC end date YYYY-MM-DD.")
@click.option("--period", default=None,
              help="UTC month, YYYY-MM. Mutually exclusive with --from/--to.")
@click.option("--format", "fmt", default="csv",
              type=click.Choice(["csv", "xlsx"], case_sensitive=False),
              show_default=True)
@click.option("-o", "--output", "output_path", default=None,
              help="Write to FILE instead of stdout. Required for --format xlsx.")
@click.pass_context
def sanctions_cmd(
    ctx: click.Context, from_str: str | None, to_str: str | None,
    period: str | None, fmt: str, output_path: str | None,
) -> None:
    """Every SEND_REJECTED whose details name the sanctions check.

    For compliance reviews. The output rows are in chronological order;
    each row preserves the full audit fields (timestamp, wallet, to,
    amount, idempotency key, client IP, token id, details).
    """
    from skr_crypto.server import reporting

    audit_path = _resolve_audit_log_path(ctx)
    from_date, to_date = _resolve_dates(period, from_str, to_str)
    try:
        reporting.validate_range(from_date, to_date)
    except ValueError as exc:
        raise SkrCryptoError(str(exc)) from exc

    hits = reporting.list_sanctions_hits(
        audit_path, from_date=from_date, to_date=to_date,
    )

    if fmt == "xlsx":
        if not output_path:
            raise SkrCryptoError(
                "--format xlsx requires -o FILE (binary on TTY mangles output)"
            )
        try:
            data = reporting.render_sanctions_xlsx(hits)
        except reporting.XlsxExtraMissing as exc:
            raise SkrCryptoError(str(exc)) from exc
        _write_or_emit(data, output_path=output_path, is_binary=True)
        return

    csv_text = reporting.render_sanctions_csv(hits)
    _write_or_emit(csv_text, output_path=output_path, is_binary=False)
