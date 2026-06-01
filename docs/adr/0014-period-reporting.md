# 0014 — Period reporting (CSV core, XLSX optional)

## Status

Proposed. 2026-05-04. Targeted for 1.9.0.

## Context

The audit log is the source of truth and is queryable line-by-
line. Operators routinely want a *summary* over a period
(month, week, ad-hoc range) — finance does monthly volume
reconciliation, compliance asks for sanctions-attempt logs,
treasury reviews top recipients quarterly. Today every such
ask is satisfied by a one-off `jq` pipeline against
`audit.log`, sometimes piped into `awk`, sometimes pasted into
a spreadsheet by hand.

This is the second-most-common operator workflow we see, after
"is the service running?". Automating it is well-defined work,
needs zero new state (audit log already holds everything), and
unlocks the UI's "Reports" tab — closing a v1.8 placeholder.

## Decision

Ship a first-party reporting module with three surfaces — CLI,
HTTP, UI — sharing one in-process aggregator that reads the
existing `AUDIT_LOG_FILE` and emits two report types:

1. **Period summary** — totals + breakdowns over a date range.
2. **Sanctions hits** — every `SEND_REJECTED` whose `details`
   names the sanctions check, over a date range.

### Report shapes

**Period summary** columns:

| Column | Meaning |
|---|---|
| `date` | UTC date (one row per day in range) |
| `send_success_count` | count of `SEND_SUCCESS` |
| `send_success_volume_usdt` | sum of `amount` for `SEND_SUCCESS` |
| `send_rejected_count` | count of `SEND_REJECTED` |
| `send_failed_count` | count of `SEND_FAILED` |
| `send_duplicate_count` | count of `SEND_DUPLICATE` |
| `receipt_resolved_success_count` | RECEIPT_RESOLVED with status=success |
| `receipt_resolved_failure_count` | RECEIPT_RESOLVED with non-success status |
| `unique_recipients` | distinct `to_address` count among SEND_SUCCESS |
| `unique_wallets` | distinct `wallet` count among SEND_SUCCESS |

Plus a footer row `TOTAL` with the sums.

**Sanctions hits** columns: `timestamp`, `wallet`, `to_address`,
`amount`, `idempotency_key`, `client_ip`, `token_id`,
`details`.

### CLI

```text
skr-crypto report --period 2026-04                # YYYY-MM (whole month, UTC)
skr-crypto report --from 2026-04-01 --to 2026-04-30
skr-crypto report --period 2026-04 --format xlsx -o april.xlsx
skr-crypto report sanctions-hits --from 2026-01-01 --to 2026-04-30
```

Defaults: `--format csv`, output to stdout. With `--format
xlsx`, `-o FILE` is required (binary). XLSX requires the
`[reports]` extra (`pip install 'skr-crypto[reports]'`); the
CLI prints a clear "install reports extra" error if openpyxl
is missing.

### HTTP

```text
GET /api/v1/reports/period?from=YYYY-MM-DD&to=YYYY-MM-DD&format=csv
GET /api/v1/reports/sanctions-hits?from=YYYY-MM-DD&to=YYYY-MM-DD&format=csv
```

`format=csv` (default): `Content-Type: text/csv`. `format=xlsx`:
`Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`,
binary body. Both endpoints require `read` scope. The XLSX
path returns 503 with a clear message if openpyxl isn't
installed.

`from`/`to` are inclusive UTC dates, max range 366 days
(prevents accidental "all time" scans on huge files).

### UI

New tab `/ui/#/reports`, visible to all authenticated tokens
(read scope is enough — these are derived from the audit data
the operator can already see).

- Date range pickers (defaults: last 30 days).
- Two buttons: "Download CSV" / "Download XLSX".
- Inline 30-day sparkline chart (volume + reject count) using
  Chart.js, vendored locally at `static/chart.min.js`. Same
  pattern as the local Alpine.js bundle.
- Below the chart, a small table preview (first 7 rows of the
  selected range) so the operator sees what they're about to
  download.
- Sanctions-hits sub-section with its own date range +
  download button.

### Module layout

```text
skr_crypto/server/reporting.py     # aggregator, format-agnostic
skr_crypto/cli/commands/report.py  # CLI entry point
skr_crypto/server/routes.py        # +2 endpoints
skr_crypto/server/ui/static/index.html, app.js, chart.min.js
                                   # UI tab + vendored Chart.js
```

The aggregator is a single function:

```python
def aggregate_period(
    audit_log_path: str, *,
    from_date: date, to_date: date,
) -> PeriodSummary:
    ...

def list_sanctions_hits(
    audit_log_path: str, *,
    from_date: date, to_date: date,
) -> list[SanctionsHit]:
    ...
```

Both stream the file once, accumulate in memory (one int /
Decimal per column per day — bounded), and return structured
data. CSV/XLSX formatting is downstream.

### XLSX as an extra, not core

`pyproject.toml`:

```toml
[project.optional-dependencies]
reports = ["openpyxl>=3.1,<4.0"]
```

CSV uses stdlib `csv`. XLSX import is lazy (inside the format
branch) so a missing extra never breaks startup or other
report flows.

### Onboarding integration

No new prompts in `skr-crypto install` — reporting works
out-of-the-box on any install with `AUDIT_LOG_FILE` set
(which the wizard always configures). `_print_next_steps`
gains one line:

```text
5. Generate a report: skr-crypto report --period $(date -u +%Y-%m)
```

`skr-crypto doctor` learns one check: if `AUDIT_LOG_FILE` is
set but the file is empty / unreadable, surface a hint that
reports will be empty until the service has run.

## Consequences

**Operators stop maintaining `jq` pipelines for routine
periodic asks.** Monthly reconciliation, compliance pulls,
quarterly recipient reviews collapse into one command or one
UI button click.

**Two reports cover ~all observed asks.** "Period summary" is
finance's monthly request. "Sanctions hits" is compliance's
recurring ask. We deliberately ship just these two; new
report types can be added file-by-file in `reporting.py`
without ADR churn.

**No new state.** Reports are read-side aggregations of the
audit log. No new tables, no new daemons. A report request is
O(audit-file-size) — the existing `/api/v1/audit` endpoint has
the same cost profile and operators have not complained.
Above ~50MB the cost becomes noticeable; documented as a
known limit, with a follow-up ADR if/when audit files routinely
exceed that.

**XLSX is opt-in.** openpyxl is ~12MB transitive (lxml etc.).
A bare `pip install skr-crypto[server]` has no spreadsheet
weight. Operators who want XLSX install
`skr-crypto[server,reports]`; the CLI/API give clear errors
otherwise.

**Chart.js is vendored, not CDN.** Same offline / air-gap
posture as the Alpine.js bundle. Adds ~70KB to the wheel.
Worth it for the dashboard the v1.8 UI implicitly promised.

**UI grows write-feeling buttons (Download).** They are not
write actions — they are GETs that produce a file, served by
the same `read` scope. The UI's "no money in the bundle"
invariant test stays unchanged (search for `/send` still
returns zero).

**Date-range cap (366 days).** Prevents accidental whole-log
scans. Operators who need longer can split the request or
operate on the audit file directly.

## Alternatives considered

- **Pre-aggregate into a daily-rollup table at write time.**
  Faster reports for huge logs. Adds a new write path and
  invalidates the "audit log is the only state" simplicity.
  Rejected for v1.9 — only worth it once we hit the 50MB
  audit threshold.
- **PDF reports.** Compliance often asks for "a PDF". The
  weight (reportlab / weasyprint / system-deps for headless
  Chrome) is enormous compared to value; CSV/XLSX exports
  are trivially convertible to PDF by the recipient.
  Rejected.
- **Custom report builder in the UI.** Drag-and-drop
  columns, etc. Massively over-scoped. Two fixed report
  shapes cover ~all observed needs.
- **One omnibus `/api/v1/report` endpoint with a `?type=`
  parameter.** Saves one route definition; loses URL clarity.
  Two endpoints read better in logs and access controls.
- **Stream CSV row-by-row to the response.** Lower memory
  for huge logs. With the 366-day cap and the daily-bucket
  shape (≤366 rows per period summary), peak memory is
  trivial. Streaming is a YAGNI optimisation.
- **Use Pandas for aggregation.** Powerful but a heavy
  dependency for what is, at most, "group by date, sum a
  Decimal column". Stdlib `csv` + a dict-of-Decimals does
  the job in ~80 lines.
- **Editable report templates in `.env`.** YAGNI; two fixed
  shapes are the contract.

## Related

- [ADR 0004](0004-audit-hard-error.md) — audit log is the
  source of truth; reporting is a derived, read-only view.
- [ADR 0007](0007-engineering-safety-practices.md) — the
  reporting code is not on the money path; it does not
  require invariant-marker tests, but the aggregator is unit-
  tested with a fixture audit log.
- [ADR 0012](0012-read-only-web-ui.md) — UI grows a Reports
  tab; no money-moving code is added; the existing UI
  invariant test is updated to whitelist the new endpoints.
- `tests/test_reporting.py` — aggregator unit tests
  (period buckets, sanctions matcher, empty-log behaviour,
  malformed lines).
- `tests/test_commands_report.py` — CLI tests (csv stdout,
  xlsx requires extra, date validation).
- `tests/test_routes.py` — HTTP tests for the two endpoints
  (auth scope, format negotiation, range validation).
