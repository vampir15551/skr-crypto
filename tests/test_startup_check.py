"""Tests for startup self-check (audit reconciliation + duplicate detection)."""
from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock

_MSK = timezone(timedelta(hours=3))


def _now_msk() -> datetime:
    return datetime.now(_MSK)


def _audit_line(
    event: str,
    *,
    txid: str = "",
    to_address: str = "",
    amount: str = "100",
    timestamp: datetime | None = None,
    idempotency_key: str = "",
) -> str:
    if timestamp is None:
        timestamp = datetime.now(UTC)
    elif timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return json.dumps({
        "id": "test-1",
        "timestamp": timestamp.astimezone(UTC).isoformat(),
        "event": event,
        "from_address": "TFromAddress",
        "to_address": to_address,
        "amount": amount,
        "asset": "USDT",
        "txid": txid,
        "idempotency_key": idempotency_key,
        "client_ip": "1.2.3.4",
        "result": "broadcast" if event == "SEND_SUCCESS" else "ok",
    })


def _setup_audit_file(tmp_path, monkeypatch, lines: list[str]):
    audit_file = tmp_path / "audit.log"
    audit_file.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(audit_file))
    import skr_crypto.server.config as cfg
    importlib.reload(cfg)
    import skr_crypto.server.audit as audit_mod
    importlib.reload(audit_mod)
    return audit_file


def _import_startup_check():
    import skr_crypto.server.startup_check as sc
    importlib.reload(sc)
    return sc


def test_skips_when_audit_log_unset(monkeypatch, caplog):
    monkeypatch.delenv("AUDIT_LOG_FILE", raising=False)
    import skr_crypto.server.config as cfg
    importlib.reload(cfg)
    import skr_crypto.server.audit as audit_mod
    importlib.reload(audit_mod)

    sc = _import_startup_check()
    with caplog.at_level("INFO", logger="payouts"):
        sc.run_startup_check()
    assert any("AUDIT_LOG_FILE not set" in r.message for r in caplog.records)


def test_skips_when_no_records_in_window(tmp_path, monkeypatch, caplog):
    # All records older than today MSK
    yesterday = _now_msk() - timedelta(days=2)
    lines = [_audit_line("SEND_SUCCESS", txid="old1", to_address="Tabc",
                         timestamp=yesterday)]
    _setup_audit_file(tmp_path, monkeypatch, lines)

    sc = _import_startup_check()
    with caplog.at_level("INFO", logger="payouts"):
        sc.run_startup_check()
    assert any("nothing to verify" in r.message for r in caplog.records)


def test_verifies_on_chain_status_for_today(tmp_path, monkeypatch, caplog):
    today = _now_msk().replace(hour=12, minute=0, second=0, microsecond=0)
    lines = [
        _audit_line("SEND_SUCCESS", txid="tx_ok", to_address="Twallet1",
                    amount="100", timestamp=today),
        _audit_line("SEND_SUCCESS", txid="tx_revert", to_address="Twallet2",
                    amount="200", timestamp=today),
    ]
    _setup_audit_file(tmp_path, monkeypatch, lines)

    sc = _import_startup_check()

    # Mock TronGrid: tx_ok = SUCCESS (no result field), tx_revert = OUT_OF_ENERGY
    def fake_get_info(txid):
        if txid == "tx_ok":
            return {"id": txid, "receipt": {}}
        if txid == "tx_revert":
            return {"id": txid, "receipt": {"result": "OUT_OF_ENERGY"}}
        return {}

    sc.tron.client = MagicMock()
    sc.tron.client.get_transaction_info = MagicMock(side_effect=fake_get_info)

    with caplog.at_level("INFO", logger="payouts"):
        sc.run_startup_check()

    msgs = [r.message for r in caplog.records]
    assert any("[STARTUP_CHECK] OK | txid=tx_ok" in m for m in msgs)
    assert any("OUT_OF_ENERGY | txid=tx_revert" in m for m in msgs)


def test_detects_duplicate_recipients(tmp_path, monkeypatch, caplog):
    today = _now_msk().replace(hour=10)
    lines = [
        _audit_line("SEND_SUCCESS", txid="tx_a", to_address="Twallet_dup",
                    amount="100", timestamp=today),
        _audit_line("SEND_SUCCESS", txid="tx_b", to_address="Twallet_dup",
                    amount="50", timestamp=today + timedelta(minutes=5)),
        _audit_line("SEND_SUCCESS", txid="tx_c", to_address="Twallet_unique",
                    amount="200", timestamp=today),
    ]
    _setup_audit_file(tmp_path, monkeypatch, lines)

    sc = _import_startup_check()
    sc.tron.client = MagicMock()
    sc.tron.client.get_transaction_info = MagicMock(return_value={"receipt": {}})

    with caplog.at_level("WARNING", logger="payouts"):
        sc.run_startup_check()

    dup_warnings = [r for r in caplog.records
                    if "DUPLICATE recipient" in r.message and "Twallet_dup" in r.message]
    assert len(dup_warnings) == 1
    assert "count=2" in dup_warnings[0].message
    # Twallet_unique must not appear in duplicate warnings
    assert not any("Twallet_unique" in r.message and "DUPLICATE" in r.message
                   for r in caplog.records)


def test_handles_malformed_audit_lines(tmp_path, monkeypatch, caplog):
    today = _now_msk().replace(hour=10)
    audit_file = tmp_path / "audit.log"
    audit_file.write_text(
        "{not valid json\n"
        + _audit_line("SEND_SUCCESS", txid="tx_ok",
                      to_address="Twallet", timestamp=today) + "\n"
        + "another garbage line\n"
    )
    monkeypatch.setenv("AUDIT_LOG_FILE", str(audit_file))
    import skr_crypto.server.config as cfg
    importlib.reload(cfg)
    import skr_crypto.server.audit as audit_mod
    importlib.reload(audit_mod)

    sc = _import_startup_check()
    sc.tron.client = MagicMock()
    sc.tron.client.get_transaction_info = MagicMock(return_value={"receipt": {}})

    # Must not raise; must log a warning per bad line and still process the good one
    with caplog.at_level("WARNING", logger="payouts"):
        sc.run_startup_check()
    assert any("unparseable" in r.message for r in caplog.records)


def test_rpc_failure_records_inconclusive_status(tmp_path, monkeypatch, caplog):
    today = _now_msk().replace(hour=10)
    lines = [_audit_line("SEND_SUCCESS", txid="tx_x", to_address="Twallet",
                         timestamp=today)]
    _setup_audit_file(tmp_path, monkeypatch, lines)

    sc = _import_startup_check()
    sc.tron.client = MagicMock()
    sc.tron.client.get_transaction_info = MagicMock(
        side_effect=RuntimeError("503 Service Unavailable")
    )

    with caplog.at_level("WARNING", logger="payouts"):
        sc.run_startup_check()

    assert any("RPC_ERROR | txid=tx_x" in r.message for r in caplog.records)


def test_ignores_non_send_success_events(tmp_path, monkeypatch):
    today = _now_msk().replace(hour=10)
    lines = [
        _audit_line("SEND_FAILED", txid="tx_failed", to_address="Tw1", timestamp=today),
        _audit_line("SEND_REJECTED", txid="", to_address="Tw2", timestamp=today),
        _audit_line("SEND_DUPLICATE", txid="tx_dup", to_address="Tw3", timestamp=today),
    ]
    _setup_audit_file(tmp_path, monkeypatch, lines)

    sc = _import_startup_check()
    sc.tron.client = MagicMock()
    sc.tron.client.get_transaction_info = MagicMock(return_value={"receipt": {}})

    sc.run_startup_check()
    # No tx_* would have been verified — TronGrid mock should not have been called
    assert sc.tron.client.get_transaction_info.call_count == 0


def test_msk_window_uses_today_midnight_msk(tmp_path, monkeypatch):
    sc = _import_startup_check()
    start = sc._msk_today_start_utc()
    # Convert back to MSK and assert it's exactly midnight
    msk = start.astimezone(_MSK)
    assert msk.hour == 0 and msk.minute == 0 and msk.second == 0
    # And it's not after now
    assert start <= datetime.now(UTC)
