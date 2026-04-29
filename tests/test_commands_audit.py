"""``skr-crypto audit`` — read + filter the audit log."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from skr_crypto.cli.main import cli


def _audit_line(event, **fields):
    base = {
        "id": "p-1",
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        "from_address": "TFrom",
        "to_address": "TTo",
        "amount": "100",
        "asset": "USDT",
        "txid": "deadbeef",
        "idempotency_key": "k-1",
        "client_ip": "1.2.3.4",
        "result": "broadcast",
    }
    base.update(fields)
    return json.dumps(base)


def _write_audit(install_dir, lines):
    (install_dir / "data").mkdir(exist_ok=True)
    (install_dir / "data" / "audit.log").write_text("\n".join(lines) + "\n")


def test_no_audit_log_warns(runner, isolated_install):
    """data/audit.log not yet present (no /send happened) — warn,
    don't crash."""
    # isolated_install has data/ but no audit.log
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "audit",
    ])
    assert result.exit_code == 0
    # Warning is on stderr; combined output mode mixes them.
    assert "No audit log" in result.stderr or "No audit log" in result.stdout


def test_audit_lists_recent(runner, isolated_install):
    _write_audit(isolated_install, [
        _audit_line("SEND_SUCCESS", txid="t1", to_address="TFirst"),
        _audit_line("SEND_SUCCESS", txid="t2", to_address="TSecond"),
    ])
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "audit",
    ])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "SEND_SUCCESS" in result.stdout
    assert "TFirst"[:12] in result.stdout
    assert "TSecond"[:12] in result.stdout


def test_audit_filter_by_event(runner, isolated_install):
    _write_audit(isolated_install, [
        _audit_line("SEND_SUCCESS", txid="ok-1"),
        _audit_line("SEND_FAILED",  txid="bad-1"),
    ])
    result = runner.invoke(cli, [
        "--dir", str(isolated_install),
        "audit", "--event", "SEND_FAILED",
    ])
    assert result.exit_code == 0
    assert "bad-1"[:12] in result.stdout
    assert "ok-1" not in result.stdout


def test_audit_filter_by_to_address(runner, isolated_install):
    _write_audit(isolated_install, [
        _audit_line("SEND_SUCCESS", txid="t1", to_address="TWantedAddr"),
        _audit_line("SEND_SUCCESS", txid="t2", to_address="TOtherAddr"),
    ])
    result = runner.invoke(cli, [
        "--dir", str(isolated_install),
        "audit", "--to", "TWantedAddr",
    ])
    assert result.exit_code == 0
    assert "TWantedAddr"[:12] in result.stdout
    assert "TOtherAddr" not in result.stdout


def test_audit_json_output_one_line_per_record(runner, isolated_install):
    _write_audit(isolated_install, [
        _audit_line("SEND_SUCCESS", txid="t1"),
        _audit_line("SEND_SUCCESS", txid="t2"),
    ])
    result = runner.invoke(cli, [
        "--dir", str(isolated_install),
        "audit", "--json",
    ])
    assert result.exit_code == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    parsed = [json.loads(ln) for ln in lines]
    assert len(parsed) == 2
    assert {p["txid"] for p in parsed} == {"t1", "t2"}


def test_audit_skips_audit_init_marker(runner, isolated_install):
    """The audit_init marker line (no 'event' key) must not break parsing
    or appear in output."""
    init_line = json.dumps({"audit_init": True, "process_id": "p1",
                            "file": "data/audit.log",
                            "timestamp": "2026-04-27T12:00:00+00:00"})
    _write_audit(isolated_install, [
        init_line,
        _audit_line("SEND_SUCCESS", txid="real-tx"),
    ])
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "audit",
    ])
    assert result.exit_code == 0
    assert "real-tx" in result.stdout


def test_audit_skips_malformed_lines(runner, isolated_install):
    """Garbage lines (e.g. partial write at crash) must not crash the
    parser; valid records still come through."""
    (isolated_install / "data").mkdir(exist_ok=True)
    (isolated_install / "data" / "audit.log").write_text(
        "{this is not json\n"
        + _audit_line("SEND_SUCCESS", txid="survives")
        + "\nmore garbage\n"
    )
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "audit",
    ])
    assert result.exit_code == 0
    assert "survives" in result.stdout


def test_audit_today_filters_by_msk(runner, isolated_install, monkeypatch):
    """--today only shows records since 00:00 MSK today."""
    yesterday = datetime(2020, 1, 1, tzinfo=UTC).isoformat()
    today_msk = datetime.now(UTC).isoformat()
    _write_audit(isolated_install, [
        _audit_line("SEND_SUCCESS", txid="old", timestamp=yesterday),
        _audit_line("SEND_SUCCESS", txid="new", timestamp=today_msk),
    ])
    result = runner.invoke(cli, [
        "--dir", str(isolated_install),
        "audit", "--today",
    ])
    assert result.exit_code == 0
    assert "new" in result.stdout
    assert "old" not in result.stdout
