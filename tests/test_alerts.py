"""Tests for the Telegram operator alerts subsystem (1.9.0). See ADR 0013.

Covers:

  - Rule evaluation (events, threshold, sanctions, quiet hours)
  - Message formatter (sanctions emphasis, no token leakage)
  - Delivery store (in-memory + sqlite enqueue/take_due/update)
  - Worker behaviour: 2xx → delivered; non-2xx → retry; budget exhausted
    → giving_up; never raises out of the worker thread
  - Audit hook does NOT block /send when alerts misbehave
  - is_configured() honours both env vars
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pytest

from skr_crypto.server import alerts

# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------


class TestRules:
    def test_sanctions_hit_detection(self):
        entry = {
            "event": "SEND_REJECTED",
            "details": "level=high failed=sanctions,burn_address",
        }
        assert alerts._is_sanctions_hit(entry) is True

    def test_sanctions_hit_case_insensitive(self):
        entry = {"event": "SEND_REJECTED", "details": "FAILED=Sanctions"}
        assert alerts._is_sanctions_hit(entry) is True

    def test_sanctions_hit_only_for_send_rejected(self):
        entry = {"event": "SEND_SUCCESS", "details": "sanctions"}
        assert alerts._is_sanctions_hit(entry) is False

    def test_threshold_decimal_compare(self):
        entry = {"event": "SEND_SUCCESS", "amount": "5000.00"}
        assert alerts._meets_threshold(entry, Decimal("4999.99")) is True
        assert alerts._meets_threshold(entry, Decimal("5000")) is True
        assert alerts._meets_threshold(entry, Decimal("5000.01")) is False

    def test_threshold_none_disables(self):
        entry = {"event": "SEND_SUCCESS", "amount": "1000000"}
        assert alerts._meets_threshold(entry, None) is False

    def test_threshold_only_for_send_success(self):
        entry = {"event": "SEND_REJECTED", "amount": "1000"}
        assert alerts._meets_threshold(entry, Decimal("1")) is False

    def test_threshold_bad_amount_returns_false(self):
        entry = {"event": "SEND_SUCCESS", "amount": "not-a-number"}
        assert alerts._meets_threshold(entry, Decimal("1")) is False

    def test_quiet_hours_simple(self):
        # Window 08-18: only those hours are quiet
        assert alerts._in_quiet_hours("08-18", 9) is True
        assert alerts._in_quiet_hours("08-18", 17) is True
        assert alerts._in_quiet_hours("08-18", 18) is False
        assert alerts._in_quiet_hours("08-18", 7) is False

    def test_quiet_hours_wraparound(self):
        # 22-08 = 22:00..23:59 + 00:00..07:59
        assert alerts._in_quiet_hours("22-08", 23) is True
        assert alerts._in_quiet_hours("22-08", 0) is True
        assert alerts._in_quiet_hours("22-08", 7) is True
        assert alerts._in_quiet_hours("22-08", 8) is False
        assert alerts._in_quiet_hours("22-08", 12) is False

    def test_quiet_hours_empty_or_malformed(self):
        assert alerts._in_quiet_hours("", 12) is False
        assert alerts._in_quiet_hours("not-a-spec", 12) is False
        assert alerts._in_quiet_hours("99-08", 12) is False
        assert alerts._in_quiet_hours("08-08", 8) is False

    def test_send_failed_bypasses_quiet_hours(self):
        assert alerts._bypasses_quiet_hours({"event": "SEND_FAILED"}) is True

    def test_sanctions_hit_bypasses_quiet_hours(self):
        entry = {"event": "SEND_REJECTED", "details": "failed=sanctions"}
        assert alerts._bypasses_quiet_hours(entry) is True

    def test_routine_event_does_not_bypass_quiet_hours(self):
        assert alerts._bypasses_quiet_hours({"event": "SEND_SUCCESS"}) is False


# ---------------------------------------------------------------------------
# Message formatter
# ---------------------------------------------------------------------------


class TestFormatter:
    def test_sanctions_message_has_emphasis(self):
        entry = {
            "event": "SEND_REJECTED",
            "wallet": "main",
            "to_address": "TXxxxxx",
            "amount": "100",
            "details": "level=high failed=sanctions",
            "timestamp": "2026-05-04T10:00:00+00:00",
        }
        msg = alerts.format_message(entry)
        assert "🚨" in msg
        assert "OFAC SANCTIONS REJECT" in msg
        assert "<code>main</code>" in msg

    def test_send_failed_message(self):
        entry = {"event": "SEND_FAILED", "wallet": "cold"}
        msg = alerts.format_message(entry)
        assert "❌" in msg
        assert "SEND FAILED" in msg

    def test_high_value_send_message(self):
        entry = {"event": "SEND_SUCCESS", "amount": "10000.00", "wallet": "hot"}
        msg = alerts.format_message(entry, threshold=Decimal("5000"))
        assert "💰" in msg
        assert "HIGH-VALUE" in msg

    def test_html_escape(self):
        entry = {"event": "SEND_FAILED", "details": "<script>alert(1)</script>"}
        msg = alerts.format_message(entry)
        assert "<script>" not in msg
        assert "&lt;script&gt;" in msg

    def test_long_details_truncated(self):
        entry = {"event": "SEND_FAILED", "details": "x" * 500}
        msg = alerts.format_message(entry)
        assert "x" * 500 not in msg
        assert "…" in msg


# ---------------------------------------------------------------------------
# Token redaction
# ---------------------------------------------------------------------------


class TestTokenRedaction:
    def test_redact_token_replaces(self):
        url = "https://api.telegram.org/bot12345:SECRET/sendMessage"
        out = alerts._redact_token(url, "12345:SECRET")
        assert "SECRET" not in out
        assert "<redacted>" in out

    def test_redact_empty_token_noop(self):
        url = "https://api.telegram.org/bot/sendMessage"
        assert alerts._redact_token(url, "") == url


# ---------------------------------------------------------------------------
# Delivery store
# ---------------------------------------------------------------------------


class TestDeliveryStore:
    def test_inmemory_enqueue_and_take_due(self):
        store = alerts.AlertDeliveryStore(db_path=None)
        i = store.enqueue(
            audit_event_id="a-1", event="SEND_FAILED",
            chat_id="100", message="hello",
        )
        assert i > 0
        due = store.take_due()
        assert len(due) == 1
        assert due[0].chat_id == "100"
        assert due[0].status == "pending"

    def test_sqlite_round_trip(self, tmp_path):
        db = tmp_path / "test.db"
        store = alerts.AlertDeliveryStore(db_path=str(db))
        i = store.enqueue(
            audit_event_id="a-2", event="SEND_REJECTED",
            chat_id="200", message="hello",
        )
        rec = store.get(i)
        assert rec is not None
        assert rec.chat_id == "200"
        assert rec.status == "pending"
        store.update_outcome(
            i, status="delivered", attempts=1, next_attempt_at=0,
            last_response_at=1.0, last_response_code=200, last_error=None,
        )
        rec2 = store.get(i)
        assert rec2.status == "delivered"
        store.close()

    def test_force_retry_only_terminal(self):
        store = alerts.AlertDeliveryStore(db_path=None)
        i = store.enqueue(
            audit_event_id="a-3", event="X", chat_id="1", message="m",
        )
        # pending → cannot force_retry
        assert store.force_retry(i) is False
        # mark giving_up → can force_retry
        store.update_outcome(
            i, status="giving_up", attempts=4, next_attempt_at=0,
            last_response_at=1.0, last_response_code=500,
            last_error="bad",
        )
        assert store.force_retry(i) is True
        rec = store.get(i)
        assert rec.status == "pending"


# ---------------------------------------------------------------------------
# Worker behaviour
# ---------------------------------------------------------------------------


class TestWorker:
    def _make_worker(self, store, *, session, backoff=(0.0, 0.0, 0.0)):
        return alerts.AlertWorker(
            store, bot_token="tok", backoff_schedule=backoff,
            timeout_sec=1.0, interval_sec=0.01, session=session,
        )

    def test_2xx_marks_delivered(self):
        store = alerts.AlertDeliveryStore(db_path=None)
        i = store.enqueue(audit_event_id="a", event="X", chat_id="1", message="m")
        sess = mock.Mock()
        sess.post.return_value = mock.Mock(status_code=200, text="ok")
        w = self._make_worker(store, session=sess)
        delivery = store.get(i)
        w._deliver_one(delivery)
        rec = store.get(i)
        assert rec.status == "delivered"
        assert rec.last_response_code == 200

    def test_non_2xx_schedules_retry(self):
        store = alerts.AlertDeliveryStore(db_path=None)
        i = store.enqueue(audit_event_id="a", event="X", chat_id="1", message="m")
        sess = mock.Mock()
        sess.post.return_value = mock.Mock(status_code=500, text="err")
        w = self._make_worker(store, session=sess, backoff=(0.0, 5.0, 30.0))
        w._deliver_one(store.get(i))
        rec = store.get(i)
        assert rec.status == "pending"
        assert rec.attempts == 1
        assert rec.last_response_code == 500

    def test_budget_exhausted_giveup(self):
        store = alerts.AlertDeliveryStore(db_path=None)
        i = store.enqueue(audit_event_id="a", event="X", chat_id="1", message="m")
        sess = mock.Mock()
        sess.post.return_value = mock.Mock(status_code=500, text="err")
        # Budget = 1 attempt allowed; second failure exhausts
        w = self._make_worker(store, session=sess, backoff=(0.0,))
        w._deliver_one(store.get(i))
        rec = store.get(i)
        assert rec.status == "giving_up"

    def test_network_exception_swallowed(self):
        store = alerts.AlertDeliveryStore(db_path=None)
        i = store.enqueue(audit_event_id="a", event="X", chat_id="1", message="m")
        sess = mock.Mock()
        sess.post.side_effect = RuntimeError("network")
        w = self._make_worker(store, session=sess, backoff=(0.0, 5.0))
        # _deliver_one MUST NOT raise
        w._deliver_one(store.get(i))
        rec = store.get(i)
        assert rec.status == "pending"
        assert "network" in (rec.last_error or "")


# ---------------------------------------------------------------------------
# notify_event integration
# ---------------------------------------------------------------------------


class TestNotifyEvent:
    @pytest.fixture(autouse=True)
    def reset_state(self):
        # Save + restore module state — tests must be isolated
        old_store = alerts.store
        old_worker = alerts._worker
        old_chat = alerts._chat_id
        old_token = alerts._bot_token
        old_filter = alerts._event_filter
        old_thresh = alerts._threshold
        old_sanc = alerts._sanctions_notify
        old_quiet = alerts._quiet_spec
        yield
        alerts.shutdown_alerts()
        alerts.store = old_store
        alerts._worker = old_worker
        alerts._chat_id = old_chat
        alerts._bot_token = old_token
        alerts._event_filter = old_filter
        alerts._threshold = old_thresh
        alerts._sanctions_notify = old_sanc
        alerts._quiet_spec = old_quiet

    def test_disabled_when_no_token(self):
        alerts.init_alerts(
            db_path=None, bot_token="", chat_id="123",
            event_filter=("SEND_FAILED",), send_threshold_usdt=None,
            sanctions_notify=True, quiet_hours_utc="",
            backoff_schedule=(0.0,), timeout_sec=1.0, interval_sec=1.0,
        )
        assert alerts.is_configured() is False
        alerts.notify_event({"id": "a-1", "event": "SEND_FAILED"})
        # No store created, nothing to enqueue. No raise.

    def test_enqueues_matching_event(self):
        alerts.init_alerts(
            db_path=None, bot_token="t", chat_id="c",
            event_filter=("SEND_FAILED",), send_threshold_usdt=None,
            sanctions_notify=True, quiet_hours_utc="",
            backoff_schedule=(0.0,), timeout_sec=1.0, interval_sec=1.0,
        )
        assert alerts.is_configured()
        alerts.notify_event({"id": "a-1", "event": "SEND_FAILED"})
        recent = alerts.store.list_recent()
        assert len(recent) == 1
        assert recent[0].event == "SEND_FAILED"

    def test_skips_non_matching_event(self):
        alerts.init_alerts(
            db_path=None, bot_token="t", chat_id="c",
            event_filter=("SEND_FAILED",), send_threshold_usdt=None,
            sanctions_notify=False, quiet_hours_utc="",
            backoff_schedule=(0.0,), timeout_sec=1.0, interval_sec=1.0,
        )
        alerts.notify_event({"id": "a-1", "event": "SEND_SUCCESS", "amount": "1"})
        assert len(alerts.store.list_recent()) == 0

    def test_threshold_triggers_send_success_alert(self):
        alerts.init_alerts(
            db_path=None, bot_token="t", chat_id="c",
            event_filter=(), send_threshold_usdt=Decimal("1000"),
            sanctions_notify=False, quiet_hours_utc="",
            backoff_schedule=(0.0,), timeout_sec=1.0, interval_sec=1.0,
        )
        alerts.notify_event({"id": "a-1", "event": "SEND_SUCCESS", "amount": "5000"})
        assert len(alerts.store.list_recent()) == 1

    def test_sanctions_hit_punches_through_quiet_hours(self, monkeypatch):
        alerts.init_alerts(
            db_path=None, bot_token="t", chat_id="c",
            event_filter=(), send_threshold_usdt=None,
            sanctions_notify=True,
            quiet_hours_utc="00-23",  # quiet basically all day
            backoff_schedule=(0.0,), timeout_sec=1.0, interval_sec=1.0,
        )
        # Sanctions hit MUST be enqueued anyway
        alerts.notify_event({
            "id": "a-1", "event": "SEND_REJECTED",
            "details": "failed=sanctions",
        })
        assert len(alerts.store.list_recent()) == 1

    def test_quiet_hours_drops_routine_event(self):
        # Build a quiet-hours spec covering the current UTC hour.
        from datetime import UTC, datetime
        cur = datetime.now(UTC).hour
        # Make a window of 1 hour starting at cur — guaranteed to match
        next_h = (cur + 1) % 24
        spec = f"{cur:02d}-{next_h:02d}"
        alerts.init_alerts(
            db_path=None, bot_token="t", chat_id="c",
            event_filter=("SEND_REJECTED",), send_threshold_usdt=None,
            sanctions_notify=False,  # so SEND_REJECTED isn't a "sanctions" exception
            quiet_hours_utc=spec,
            backoff_schedule=(0.0,), timeout_sec=1.0, interval_sec=1.0,
        )
        alerts.notify_event({
            "id": "a-1", "event": "SEND_REJECTED",
            "details": "level=high failed=balance",  # not sanctions
        })
        assert len(alerts.store.list_recent()) == 0

    def test_enqueue_failure_does_not_raise(self, monkeypatch):
        """The audit hook must NEVER raise into the /send path."""
        alerts.init_alerts(
            db_path=None, bot_token="t", chat_id="c",
            event_filter=("SEND_FAILED",), send_threshold_usdt=None,
            sanctions_notify=False, quiet_hours_utc="",
            backoff_schedule=(0.0,), timeout_sec=1.0, interval_sec=1.0,
        )
        # Force the store to raise on enqueue
        monkeypatch.setattr(
            alerts.store, "enqueue",
            mock.Mock(side_effect=RuntimeError("boom")),
        )
        # Must not raise
        alerts.notify_event({"id": "a-1", "event": "SEND_FAILED"})
