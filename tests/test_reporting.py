"""Tests for the reporting aggregator (1.9.0). See ADR 0014.

Covers:

  - Period parsing (YYYY-MM)
  - Range validation (rejects backwards / oversized ranges)
  - Aggregation correctness (counts, volume, unique sets, daily buckets)
  - Sanctions-hit matcher (substring, only SEND_REJECTED)
  - CSV rendering (header + rows + TOTAL footer)
  - Empty / missing / malformed audit log behaviour
  - Sparkline shape
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from skr_crypto.server import reporting


def _write_audit(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


# ---------------------------------------------------------------------------
# Period / range helpers
# ---------------------------------------------------------------------------


class TestPeriodParsing:
    def test_parse_full_month(self):
        f, t = reporting.parse_period_yyyymm("2026-04")
        assert f == date(2026, 4, 1)
        assert t == date(2026, 4, 30)

    def test_parse_february_leap(self):
        _f, t = reporting.parse_period_yyyymm("2024-02")
        assert t == date(2024, 2, 29)

    def test_parse_december_wrap(self):
        _f, t = reporting.parse_period_yyyymm("2026-12")
        assert t == date(2026, 12, 31)

    def test_invalid_month(self):
        with pytest.raises(ValueError):
            reporting.parse_period_yyyymm("2026-13")

    def test_bad_format(self):
        with pytest.raises(ValueError):
            reporting.parse_period_yyyymm("April 2026")


class TestRangeValidation:
    def test_rejects_backwards(self):
        with pytest.raises(ValueError):
            reporting.validate_range(date(2026, 4, 30), date(2026, 4, 1))

    def test_rejects_oversize(self):
        with pytest.raises(ValueError):
            reporting.validate_range(date(2024, 1, 1), date(2026, 12, 31))

    def test_accepts_max_size(self):
        # 366 days exactly
        reporting.validate_range(date(2024, 1, 1), date(2024, 12, 31))


# ---------------------------------------------------------------------------
# Period aggregation
# ---------------------------------------------------------------------------


class TestAggregatePeriod:
    def test_empty_log(self, tmp_path):
        log = tmp_path / "audit.log"
        log.write_text("")
        s = reporting.aggregate_period(
            str(log), from_date=date(2026, 5, 1), to_date=date(2026, 5, 3),
        )
        assert s.total_send_success_count == 0
        assert len(s.buckets) == 3  # one bucket per day even if empty

    def test_missing_log_treated_as_empty(self, tmp_path):
        # Path that doesn't exist
        s = reporting.aggregate_period(
            str(tmp_path / "nope.log"),
            from_date=date(2026, 5, 1), to_date=date(2026, 5, 1),
        )
        assert s.total_send_success_count == 0

    def test_buckets_filled_correctly(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_audit(log, [
            {"id": "1", "event": "SEND_SUCCESS", "amount": "10.50",
             "wallet": "main", "to_address": "T1",
             "timestamp": "2026-05-01T10:00:00+00:00"},
            {"id": "2", "event": "SEND_SUCCESS", "amount": "100",
             "wallet": "main", "to_address": "T2",
             "timestamp": "2026-05-01T11:00:00+00:00"},
            {"id": "3", "event": "SEND_REJECTED", "amount": "5",
             "wallet": "main", "to_address": "T3",
             "timestamp": "2026-05-02T10:00:00+00:00"},
            {"id": "4", "event": "SEND_FAILED", "amount": "1",
             "wallet": "cold", "to_address": "T4",
             "timestamp": "2026-05-03T10:00:00+00:00"},
            {"id": "5", "event": "SEND_DUPLICATE",
             "wallet": "main", "to_address": "T1",
             "timestamp": "2026-05-03T11:00:00+00:00"},
        ])
        s = reporting.aggregate_period(
            str(log),
            from_date=date(2026, 5, 1), to_date=date(2026, 5, 3),
        )
        # Day 1: 2 sends, 110.50 USDT, 2 unique recipients
        b1 = s.buckets[0]
        assert b1.send_success_count == 2
        assert b1.send_success_volume_usdt == Decimal("110.50")
        assert len(b1.unique_recipients) == 2
        assert len(b1.unique_wallets) == 1
        # Day 2: 1 reject
        assert s.buckets[1].send_rejected_count == 1
        # Day 3: 1 fail + 1 dup
        assert s.buckets[2].send_failed_count == 1
        assert s.buckets[2].send_duplicate_count == 1

        # Totals
        assert s.total_send_success_count == 2
        assert s.total_send_success_volume_usdt == Decimal("110.50")
        assert s.total_send_rejected_count == 1
        assert s.total_send_failed_count == 1
        assert s.total_send_duplicate_count == 1
        assert s.total_unique_recipients == 2
        assert s.total_unique_wallets == 1

    def test_records_outside_range_ignored(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_audit(log, [
            {"id": "1", "event": "SEND_SUCCESS", "amount": "1",
             "wallet": "w", "to_address": "T",
             "timestamp": "2026-01-01T10:00:00+00:00"},
            {"id": "2", "event": "SEND_SUCCESS", "amount": "1",
             "wallet": "w", "to_address": "T",
             "timestamp": "2026-12-31T10:00:00+00:00"},
        ])
        s = reporting.aggregate_period(
            str(log),
            from_date=date(2026, 5, 1), to_date=date(2026, 5, 31),
        )
        assert s.total_send_success_count == 0

    def test_malformed_lines_skipped(self, tmp_path):
        log = tmp_path / "audit.log"
        log.write_text(
            json.dumps({"id": "1", "event": "SEND_SUCCESS", "amount": "5",
                        "wallet": "w", "to_address": "T",
                        "timestamp": "2026-05-01T10:00:00+00:00"})
            + "\nNOT JSON LINE\n"
            + json.dumps({"id": "2", "event": "SEND_SUCCESS", "amount": "5",
                          "wallet": "w", "to_address": "T",
                          "timestamp": "2026-05-01T11:00:00+00:00"})
            + "\n"
        )
        s = reporting.aggregate_period(
            str(log), from_date=date(2026, 5, 1), to_date=date(2026, 5, 1),
        )
        assert s.total_send_success_count == 2

    def test_receipt_resolved_split_by_result(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_audit(log, [
            {"id": "1", "event": "RECEIPT_RESOLVED", "result": "success",
             "timestamp": "2026-05-01T10:00:00+00:00"},
            {"id": "2", "event": "RECEIPT_RESOLVED", "result": "out_of_energy",
             "timestamp": "2026-05-01T11:00:00+00:00"},
            {"id": "3", "event": "RECEIPT_RESOLVED", "result": "revert",
             "timestamp": "2026-05-01T12:00:00+00:00"},
        ])
        s = reporting.aggregate_period(
            str(log), from_date=date(2026, 5, 1), to_date=date(2026, 5, 1),
        )
        assert s.buckets[0].receipt_resolved_success_count == 1
        assert s.buckets[0].receipt_resolved_failure_count == 2


# ---------------------------------------------------------------------------
# Sanctions hits
# ---------------------------------------------------------------------------


class TestSanctionsHits:
    def test_only_send_rejected_with_sanctions(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_audit(log, [
            {"id": "1", "event": "SEND_REJECTED",
             "details": "level=high failed=sanctions",
             "wallet": "w", "to_address": "T_BAD", "amount": "100",
             "timestamp": "2026-05-01T10:00:00+00:00"},
            {"id": "2", "event": "SEND_REJECTED",
             "details": "level=high failed=balance_low",
             "wallet": "w", "to_address": "T_OK", "amount": "1",
             "timestamp": "2026-05-01T11:00:00+00:00"},
            # not SEND_REJECTED
            {"id": "3", "event": "SEND_SUCCESS",
             "details": "sanctions",
             "wallet": "w", "to_address": "T", "amount": "1",
             "timestamp": "2026-05-01T12:00:00+00:00"},
        ])
        hits = reporting.list_sanctions_hits(
            str(log), from_date=date(2026, 5, 1), to_date=date(2026, 5, 1),
        )
        assert len(hits) == 1
        assert hits[0].to_address == "T_BAD"

    def test_chronological_order(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_audit(log, [
            {"id": "2", "event": "SEND_REJECTED", "details": "failed=sanctions",
             "to_address": "TB", "wallet": "w", "amount": "1",
             "timestamp": "2026-05-02T10:00:00+00:00"},
            {"id": "1", "event": "SEND_REJECTED", "details": "failed=sanctions",
             "to_address": "TA", "wallet": "w", "amount": "1",
             "timestamp": "2026-05-01T10:00:00+00:00"},
        ])
        hits = reporting.list_sanctions_hits(
            str(log), from_date=date(2026, 5, 1), to_date=date(2026, 5, 31),
        )
        assert [h.to_address for h in hits] == ["TA", "TB"]


# ---------------------------------------------------------------------------
# CSV rendering
# ---------------------------------------------------------------------------


class TestCsvRendering:
    def test_period_csv_has_header_and_total(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_audit(log, [
            {"id": "1", "event": "SEND_SUCCESS", "amount": "10",
             "wallet": "w", "to_address": "T",
             "timestamp": "2026-05-01T10:00:00+00:00"},
        ])
        s = reporting.aggregate_period(
            str(log), from_date=date(2026, 5, 1), to_date=date(2026, 5, 1),
        )
        csv_text = reporting.render_period_csv(s)
        lines = csv_text.strip().splitlines()
        assert lines[0].startswith("date,send_success_count")
        assert lines[-1].startswith("TOTAL,")
        # 1 header + 1 day + 1 total = 3 lines
        assert len(lines) == 3

    def test_sanctions_csv_round_trip(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_audit(log, [
            {"id": "1", "event": "SEND_REJECTED", "details": "failed=sanctions",
             "to_address": "T_BAD", "wallet": "w", "amount": "100",
             "idempotency_key": "k", "client_ip": "1.2.3.4",
             "token_id": "t",
             "timestamp": "2026-05-01T10:00:00+00:00"},
        ])
        hits = reporting.list_sanctions_hits(
            str(log), from_date=date(2026, 5, 1), to_date=date(2026, 5, 1),
        )
        csv_text = reporting.render_sanctions_csv(hits)
        assert "T_BAD" in csv_text
        assert "1.2.3.4" in csv_text
        assert "failed=sanctions" in csv_text


# ---------------------------------------------------------------------------
# Sparkline series
# ---------------------------------------------------------------------------


class TestSparkline:
    def test_shape(self, tmp_path):
        log = tmp_path / "audit.log"
        _write_audit(log, [
            {"id": "1", "event": "SEND_SUCCESS", "amount": "10",
             "wallet": "w", "to_address": "T",
             "timestamp": "2026-05-01T10:00:00+00:00"},
        ])
        s = reporting.aggregate_period(
            str(log), from_date=date(2026, 5, 1), to_date=date(2026, 5, 3),
        )
        spark = reporting.sparkline_series(s)
        assert len(spark["dates"]) == 3
        assert len(spark["send_success_volume_usdt"]) == 3
        assert spark["send_success_volume_usdt"][0] == 10.0
        assert spark["send_success_volume_usdt"][1] == 0.0


# ---------------------------------------------------------------------------
# XLSX extra
# ---------------------------------------------------------------------------


class TestXlsxExtra:
    def test_xlsx_extra_missing_raises_clearly(self, tmp_path, monkeypatch):
        log = tmp_path / "audit.log"
        log.write_text("")
        s = reporting.aggregate_period(
            str(log), from_date=date(2026, 5, 1), to_date=date(2026, 5, 1),
        )
        # Force the import to fail
        import builtins
        original_import = builtins.__import__

        def blocked_import(name, *args, **kwargs):
            if name == "openpyxl":
                raise ImportError("test: openpyxl not installed")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        with pytest.raises(reporting.XlsxExtraMissing):
            reporting.render_period_xlsx(s)
