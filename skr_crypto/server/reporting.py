"""Period reporting over the durable audit log. See ADR 0014.

Two report types share one in-process aggregator that reads
``AUDIT_LOG_FILE`` and accumulates daily buckets:

  - **Period summary** — counts + USDT volume per UTC day, plus a
    TOTAL footer. Used by finance for monthly reconciliation.
  - **Sanctions hits** — every ``SEND_REJECTED`` whose ``details``
    names the sanctions check, in chronological order.

The aggregator is format-agnostic — it returns structured data.
CSV is rendered via stdlib ``csv``; XLSX via openpyxl, lazy-imported
inside the format branch so a missing ``[reports]`` extra never
breaks startup or other report flows.

Cost: O(audit-file-size) per request. With the ``MAX_REPORT_DAYS``
cap (366) and one accumulator per day, peak memory is bounded
regardless of file size — the file is streamed once.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

log = logging.getLogger("payouts")


# Maximum range a single report can cover. Operators with longer
# horizons should split the request or operate on the audit file
# directly (jq / awk). Documented in ADR 0014.
MAX_REPORT_DAYS = 366


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class DayBucket:
    date: date
    send_success_count: int = 0
    send_success_volume_usdt: Decimal = Decimal(0)
    send_rejected_count: int = 0
    send_failed_count: int = 0
    send_duplicate_count: int = 0
    receipt_resolved_success_count: int = 0
    receipt_resolved_failure_count: int = 0
    unique_recipients: set[str] = field(default_factory=set)
    unique_wallets: set[str] = field(default_factory=set)


@dataclass
class PeriodSummary:
    """All daily buckets in the requested range, plus a TOTAL row.

    ``buckets`` is sorted ascending by date and contains exactly one
    entry per day in [from_date, to_date] inclusive — even days with
    zero events get a row so a CSV reader sees a continuous timeline.
    """
    from_date: date
    to_date: date
    buckets: list[DayBucket]
    total_send_success_count: int
    total_send_success_volume_usdt: Decimal
    total_send_rejected_count: int
    total_send_failed_count: int
    total_send_duplicate_count: int
    total_receipt_resolved_success_count: int
    total_receipt_resolved_failure_count: int
    total_unique_recipients: int
    total_unique_wallets: int


@dataclass
class SanctionsHit:
    timestamp: str
    wallet: str
    to_address: str
    amount: str
    idempotency_key: str
    client_ip: str
    token_id: str
    details: str


# ---------------------------------------------------------------------------
# Range helpers
# ---------------------------------------------------------------------------


def parse_period_yyyymm(value: str) -> tuple[date, date]:
    """Parse ``YYYY-MM`` into the (first, last) UTC dates of that month."""
    parts = value.split("-")
    if len(parts) != 2:
        raise ValueError(f"period must be YYYY-MM, got {value!r}")
    year = int(parts[0])
    month = int(parts[1])
    if not (1 <= month <= 12):
        raise ValueError(f"month out of range: {month}")
    first = date(year, month, 1)
    last = (
        date(year, 12, 31) if month == 12
        else date(year, month + 1, 1) - timedelta(days=1)
    )
    return first, last


def validate_range(from_date: date, to_date: date) -> None:
    if to_date < from_date:
        raise ValueError(f"to_date {to_date} is before from_date {from_date}")
    span = (to_date - from_date).days + 1
    if span > MAX_REPORT_DAYS:
        raise ValueError(
            f"date range {span} days exceeds MAX_REPORT_DAYS={MAX_REPORT_DAYS}; "
            f"split the request or read audit.log directly"
        )


def _daterange(from_date: date, to_date: date) -> Iterator[date]:
    cur = from_date
    while cur <= to_date:
        yield cur
        cur += timedelta(days=1)


# ---------------------------------------------------------------------------
# Audit reader
# ---------------------------------------------------------------------------


def _iter_audit_records(audit_log_path: str | None) -> Iterator[dict]:
    """Yield audit records as dicts. Empty if file is missing / unreadable.

    Malformed (non-JSON) lines are skipped silently — same posture as
    the /api/v1/audit endpoint.
    """
    if not audit_log_path or not os.path.exists(audit_log_path):
        return
    try:
        with open(audit_log_path, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        log.warning("[REPORT] audit read failed: %s", exc)
        return


def _record_date(rec: dict) -> date | None:
    """Extract the UTC date from an audit record's ``timestamp`` field."""
    ts = rec.get("timestamp")
    if not ts:
        return None
    try:
        # Audit writes UTC isoformat with explicit offset (+00:00).
        return datetime.fromisoformat(ts).astimezone(UTC).date()
    except (ValueError, TypeError):
        return None


def _safe_decimal(value) -> Decimal:
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


# ---------------------------------------------------------------------------
# Aggregator: period summary
# ---------------------------------------------------------------------------


def aggregate_period(
    audit_log_path: str | None,
    *,
    from_date: date,
    to_date: date,
) -> PeriodSummary:
    """Stream the audit log once and produce a PeriodSummary."""
    validate_range(from_date, to_date)

    buckets: dict[date, DayBucket] = {
        d: DayBucket(date=d) for d in _daterange(from_date, to_date)
    }

    for rec in _iter_audit_records(audit_log_path):
        d = _record_date(rec)
        if d is None or d < from_date or d > to_date:
            continue
        bucket = buckets[d]
        event = rec.get("event")

        if event == "SEND_SUCCESS":
            bucket.send_success_count += 1
            bucket.send_success_volume_usdt += _safe_decimal(rec.get("amount"))
            if rec.get("to_address"):
                bucket.unique_recipients.add(rec["to_address"])
            if rec.get("wallet"):
                bucket.unique_wallets.add(rec["wallet"])
        elif event == "SEND_REJECTED":
            bucket.send_rejected_count += 1
        elif event == "SEND_FAILED":
            bucket.send_failed_count += 1
        elif event == "SEND_DUPLICATE":
            bucket.send_duplicate_count += 1
        elif event == "RECEIPT_RESOLVED":
            result = (rec.get("result") or "").lower()
            if result == "success":
                bucket.receipt_resolved_success_count += 1
            else:
                bucket.receipt_resolved_failure_count += 1

    sorted_buckets = [buckets[d] for d in _daterange(from_date, to_date)]
    all_recipients: set[str] = set()
    all_wallets: set[str] = set()
    for b in sorted_buckets:
        all_recipients.update(b.unique_recipients)
        all_wallets.update(b.unique_wallets)

    return PeriodSummary(
        from_date=from_date,
        to_date=to_date,
        buckets=sorted_buckets,
        total_send_success_count=sum(b.send_success_count for b in sorted_buckets),
        total_send_success_volume_usdt=sum(
            (b.send_success_volume_usdt for b in sorted_buckets), Decimal(0),
        ),
        total_send_rejected_count=sum(b.send_rejected_count for b in sorted_buckets),
        total_send_failed_count=sum(b.send_failed_count for b in sorted_buckets),
        total_send_duplicate_count=sum(b.send_duplicate_count for b in sorted_buckets),
        total_receipt_resolved_success_count=sum(
            b.receipt_resolved_success_count for b in sorted_buckets
        ),
        total_receipt_resolved_failure_count=sum(
            b.receipt_resolved_failure_count for b in sorted_buckets
        ),
        total_unique_recipients=len(all_recipients),
        total_unique_wallets=len(all_wallets),
    )


# ---------------------------------------------------------------------------
# Aggregator: sanctions hits
# ---------------------------------------------------------------------------


def list_sanctions_hits(
    audit_log_path: str | None,
    *,
    from_date: date,
    to_date: date,
) -> list[SanctionsHit]:
    """Return every SEND_REJECTED with a sanctions match in [from, to]."""
    validate_range(from_date, to_date)
    out: list[SanctionsHit] = []
    for rec in _iter_audit_records(audit_log_path):
        if rec.get("event") != "SEND_REJECTED":
            continue
        details = (rec.get("details") or "").lower()
        if "sanctions" not in details:
            continue
        d = _record_date(rec)
        if d is None or d < from_date or d > to_date:
            continue
        out.append(SanctionsHit(
            timestamp=rec.get("timestamp", ""),
            wallet=rec.get("wallet", ""),
            to_address=rec.get("to_address", ""),
            amount=str(rec.get("amount", "")),
            idempotency_key=rec.get("idempotency_key", ""),
            client_ip=rec.get("client_ip", ""),
            token_id=rec.get("token_id", ""),
            details=rec.get("details", ""),
        ))
    out.sort(key=lambda h: h.timestamp)
    return out


# ---------------------------------------------------------------------------
# CSV rendering
# ---------------------------------------------------------------------------


_PERIOD_COLUMNS = [
    "date",
    "send_success_count",
    "send_success_volume_usdt",
    "send_rejected_count",
    "send_failed_count",
    "send_duplicate_count",
    "receipt_resolved_success_count",
    "receipt_resolved_failure_count",
    "unique_recipients",
    "unique_wallets",
]

_SANCTIONS_COLUMNS = [
    "timestamp",
    "wallet",
    "to_address",
    "amount",
    "idempotency_key",
    "client_ip",
    "token_id",
    "details",
]


def render_period_csv(summary: PeriodSummary) -> str:
    """Render a PeriodSummary as a CSV string (UTF-8, \\r\\n endings)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(_PERIOD_COLUMNS)
    for b in summary.buckets:
        writer.writerow([
            b.date.isoformat(),
            b.send_success_count,
            str(b.send_success_volume_usdt),
            b.send_rejected_count,
            b.send_failed_count,
            b.send_duplicate_count,
            b.receipt_resolved_success_count,
            b.receipt_resolved_failure_count,
            len(b.unique_recipients),
            len(b.unique_wallets),
        ])
    writer.writerow([
        "TOTAL",
        summary.total_send_success_count,
        str(summary.total_send_success_volume_usdt),
        summary.total_send_rejected_count,
        summary.total_send_failed_count,
        summary.total_send_duplicate_count,
        summary.total_receipt_resolved_success_count,
        summary.total_receipt_resolved_failure_count,
        summary.total_unique_recipients,
        summary.total_unique_wallets,
    ])
    return buf.getvalue()


def render_sanctions_csv(hits: list[SanctionsHit]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(_SANCTIONS_COLUMNS)
    for h in hits:
        writer.writerow([
            h.timestamp, h.wallet, h.to_address, h.amount,
            h.idempotency_key, h.client_ip, h.token_id, h.details,
        ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# XLSX rendering (opt-in via [reports] extra)
# ---------------------------------------------------------------------------


class XlsxExtraMissing(Exception):
    """Raised when XLSX output is requested but openpyxl isn't installed."""


def _require_openpyxl():
    try:
        import openpyxl  # noqa: F401
    except ImportError as exc:
        raise XlsxExtraMissing(
            "XLSX output requires the [reports] extra. "
            "Install with: pip install 'skr-crypto[server,reports]'"
        ) from exc


def render_period_xlsx(summary: PeriodSummary) -> bytes:
    """Render a PeriodSummary as XLSX bytes. Requires [reports] extra."""
    _require_openpyxl()
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = f"Period {summary.from_date}..{summary.to_date}"
    ws.append(_PERIOD_COLUMNS)
    for b in summary.buckets:
        ws.append([
            b.date.isoformat(),
            b.send_success_count,
            float(b.send_success_volume_usdt),
            b.send_rejected_count,
            b.send_failed_count,
            b.send_duplicate_count,
            b.receipt_resolved_success_count,
            b.receipt_resolved_failure_count,
            len(b.unique_recipients),
            len(b.unique_wallets),
        ])
    ws.append([
        "TOTAL",
        summary.total_send_success_count,
        float(summary.total_send_success_volume_usdt),
        summary.total_send_rejected_count,
        summary.total_send_failed_count,
        summary.total_send_duplicate_count,
        summary.total_receipt_resolved_success_count,
        summary.total_receipt_resolved_failure_count,
        summary.total_unique_recipients,
        summary.total_unique_wallets,
    ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render_sanctions_xlsx(hits: list[SanctionsHit]) -> bytes:
    _require_openpyxl()
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Sanctions hits"
    ws.append(_SANCTIONS_COLUMNS)
    for h in hits:
        ws.append([
            h.timestamp, h.wallet, h.to_address, h.amount,
            h.idempotency_key, h.client_ip, h.token_id, h.details,
        ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
CSV_CONTENT_TYPE = "text/csv; charset=utf-8"


# ---------------------------------------------------------------------------
# Sparkline data helper (UI-side)
# ---------------------------------------------------------------------------


def sparkline_series(summary: PeriodSummary) -> dict:
    """Return a small dict the UI can plot as inline-SVG sparklines.

    Only structured numbers, no formatting decisions — those belong
    in the UI.
    """
    return {
        "dates": [b.date.isoformat() for b in summary.buckets],
        "send_success_volume_usdt": [
            float(b.send_success_volume_usdt) for b in summary.buckets
        ],
        "send_success_count": [b.send_success_count for b in summary.buckets],
        "send_rejected_count": [b.send_rejected_count for b in summary.buckets],
    }
