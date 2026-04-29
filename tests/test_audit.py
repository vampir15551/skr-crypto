from __future__ import annotations

import json
import logging
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from skr_crypto.server.audit import record as audit_record

VALID_ADDRESS = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"


class TestAuditRecord:
    """Verify audit.record() writes structured JSON with all required fields."""

    @pytest.fixture(autouse=True)
    def capture_audit(self, caplog):
        self.caplog = caplog

    def _last_audit_entry(self) -> dict:
        """Parse the last audit log line as JSON."""
        for r in reversed(self.caplog.records):
            if r.name == "payouts.audit":
                return json.loads(r.getMessage())
        raise AssertionError("No audit log entry found")

    def test_send_success_audit(self, caplog):
        with caplog.at_level(logging.INFO, logger="payouts.audit"):
            audit_record(
                "SEND_SUCCESS",
                from_address="TFrom123",
                to_address="TTo456",
                amount="100.50",
                txid="abc123",
                idempotency_key="key-1",
                client_ip="10.0.0.1",
                result="broadcast",
                details="elapsed=0.35s",
            )
        entry = self._last_audit_entry()
        assert entry["event"] == "SEND_SUCCESS"
        assert entry["from_address"] == "TFrom123"
        assert entry["to_address"] == "TTo456"
        assert entry["amount"] == "100.50"
        assert entry["asset"] == "USDT"
        assert entry["txid"] == "abc123"
        assert entry["idempotency_key"] == "key-1"
        assert entry["client_ip"] == "10.0.0.1"
        assert entry["result"] == "broadcast"
        assert entry["details"] == "elapsed=0.35s"
        # New format: "<process_uuid_prefix>-<seq>" instead of numeric "seq".
        assert "id" in entry
        assert "-" in entry["id"]
        assert "timestamp" in entry

    def test_send_rejected_audit(self, caplog):
        with caplog.at_level(logging.INFO, logger="payouts.audit"):
            audit_record(
                "SEND_REJECTED",
                from_address="TFrom",
                to_address="TTo",
                amount="999",
                idempotency_key="key-2",
                client_ip="10.0.0.2",
                result="insufficient_usdt",
                details="have=50",
            )
        entry = self._last_audit_entry()
        assert entry["event"] == "SEND_REJECTED"
        assert entry["result"] == "insufficient_usdt"
        assert entry["txid"] == ""  # no tx was created

    def test_id_increments_within_process(self, caplog):
        with caplog.at_level(logging.INFO, logger="payouts.audit"):
            audit_record("EVT_A", result="ok")
            e1 = self._last_audit_entry()
            audit_record("EVT_B", result="ok")
            e2 = self._last_audit_entry()
        # Same process prefix, strictly increasing seq
        p1, s1 = e1["id"].rsplit("-", 1)
        p2, s2 = e2["id"].rsplit("-", 1)
        assert p1 == p2, "process prefix must be stable within one run"
        assert int(s2) > int(s1)

    def test_entry_is_valid_json(self, caplog):
        with caplog.at_level(logging.INFO, logger="payouts.audit"):
            audit_record("TEST_JSON", amount="1.23", result="ok")
        for r in self.caplog.records:
            if r.name == "payouts.audit":
                # Must parse without error
                data = json.loads(r.getMessage())
                assert isinstance(data, dict)


class TestAuditInRoutes:
    """Verify that routes actually emit audit records."""

    def test_send_success_emits_audit(self, client, auth_headers, mock_tron, caplog):
        with caplog.at_level(logging.INFO, logger="payouts.audit"):
            resp = client.post("/api/v1/send", json={
                "to_address": VALID_ADDRESS,
                "amount": "50",
                "idempotency_key": "audit-test-1",
            }, headers=auth_headers)

        assert resp.status_code == 200
        audit_entries = [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == "payouts.audit"
        ]
        assert len(audit_entries) == 1
        assert audit_entries[0]["event"] == "SEND_SUCCESS"
        assert audit_entries[0]["txid"] == "abc123txid"

    def test_send_duplicate_emits_audit(self, client, auth_headers, mock_tron, caplog):
        # First call
        client.post("/api/v1/send", json={
            "to_address": VALID_ADDRESS,
            "amount": "50",
            "idempotency_key": "audit-dup-key",
        }, headers=auth_headers)

        # Second call — duplicate
        with caplog.at_level(logging.INFO, logger="payouts.audit"):
            caplog.clear()
            resp = client.post("/api/v1/send", json={
                "to_address": VALID_ADDRESS,
                "amount": "50",
                "idempotency_key": "audit-dup-key",
            }, headers=auth_headers)

        assert resp.status_code == 200
        audit_entries = [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == "payouts.audit"
        ]
        assert any(e["event"] == "SEND_DUPLICATE" for e in audit_entries)

    def test_send_rejected_emits_audit(self, client, auth_headers, mock_tron, caplog):
        mock_tron.get_usdt_balance = MagicMock(return_value=Decimal("1"))

        with caplog.at_level(logging.INFO, logger="payouts.audit"):
            resp = client.post("/api/v1/send", json={
                "to_address": VALID_ADDRESS,
                "amount": "1000",
                "idempotency_key": "audit-reject-key",
            }, headers=auth_headers)

        assert resp.status_code == 400
        audit_entries = [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == "payouts.audit"
        ]
        assert any(e["event"] == "SEND_REJECTED" for e in audit_entries)

    def test_send_failed_emits_audit(self, client, auth_headers, mock_tron, caplog):
        mock_tron.send_usdt = MagicMock(side_effect=RuntimeError("node timeout"))

        with caplog.at_level(logging.INFO, logger="payouts.audit"):
            resp = client.post("/api/v1/send", json={
                "to_address": VALID_ADDRESS,
                "amount": "10",
                "idempotency_key": "audit-fail-key",
            }, headers=auth_headers)

        assert resp.status_code == 500
        audit_entries = [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == "payouts.audit"
        ]
        assert any(e["event"] == "SEND_FAILED" for e in audit_entries)
