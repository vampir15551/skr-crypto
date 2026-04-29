from __future__ import annotations

import json
import logging

from skr_crypto.server.logging_config import JSONFormatter


class TestJSONFormatter:
    def _make_record(self, msg: str = "test message", level: int = logging.INFO) -> logging.LogRecord:
        return logging.LogRecord(
            name="payouts",
            level=level,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_output_is_valid_json(self):
        fmt = JSONFormatter()
        record = self._make_record()
        result = fmt.format(record)
        data = json.loads(result)
        assert isinstance(data, dict)

    def test_required_fields_present(self):
        fmt = JSONFormatter()
        record = self._make_record("hello world")
        data = json.loads(fmt.format(record))
        assert data["message"] == "hello world"
        assert data["level"] == "INFO"
        assert data["logger"] == "payouts"
        assert "timestamp" in data

    def test_timestamp_is_iso8601(self):
        fmt = JSONFormatter()
        record = self._make_record()
        data = json.loads(fmt.format(record))
        ts = data["timestamp"]
        # ISO 8601 with timezone offset
        assert "T" in ts
        assert ts.endswith("+00:00") or ts.endswith("Z")

    def test_extra_fields_included(self):
        fmt = JSONFormatter()
        record = self._make_record()
        record.request_id = "req-abc"
        record.txid = "tx-123"
        data = json.loads(fmt.format(record))
        assert data["request_id"] == "req-abc"
        assert data["txid"] == "tx-123"

    def test_extra_fields_absent_when_not_set(self):
        fmt = JSONFormatter()
        record = self._make_record()
        data = json.loads(fmt.format(record))
        assert "request_id" not in data
        assert "txid" not in data

    def test_exception_included(self):
        fmt = JSONFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = self._make_record()
            record.exc_info = sys.exc_info()
        data = json.loads(fmt.format(record))
        assert "exception" in data
        assert "ValueError: boom" in data["exception"]

    def test_error_level(self):
        fmt = JSONFormatter()
        record = self._make_record("err", logging.ERROR)
        data = json.loads(fmt.format(record))
        assert data["level"] == "ERROR"

    def test_unicode_message(self):
        fmt = JSONFormatter()
        record = self._make_record("Перевод USDT на адрес")
        data = json.loads(fmt.format(record))
        assert "USDT" in data["message"]
