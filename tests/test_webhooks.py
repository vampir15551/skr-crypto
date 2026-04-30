"""Tests for the opt-in webhook subsystem (1.7.0).

Covers:

  - HMAC signing + verification (constant-time, timestamp-bounded)
  - Delivery store CRUD (in-memory + SQLite-backed)
  - Worker retry policy (success / fail / giveup / WEBHOOK_GIVEUP audit)
  - The /send hot path is NOT blocked by webhook plumbing (invariant)
  - Event filter (only configured events trigger delivery)
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest
import responses

from skr_crypto.server import webhooks as wh

# ---------------------------------------------------------------------------
# HMAC signing
# ---------------------------------------------------------------------------


class TestSignVerify:
    def test_sign_produces_t_and_v1_parts(self):
        h = wh.sign_payload("secret", 1234567890, '{"k":1}')
        assert h.startswith("t=1234567890,v1=")
        assert len(h.split(",v1=")[1]) == 64  # sha256 hex

    @pytest.mark.invariant
    def test_verify_returns_true_for_valid(self):
        """INVARIANT: a payload signed with the right secret + recent
        timestamp must verify. If this regresses, every receiver
        rejects every delivery."""
        body = '{"event":"SEND_SUCCESS"}'
        ts = int(time.time())
        sig = wh.sign_payload("topsecret", ts, body)
        assert wh.verify_signature("topsecret", sig, body) is True

    @pytest.mark.invariant
    def test_verify_rejects_wrong_secret(self):
        body = '{"x": 1}'
        ts = int(time.time())
        sig = wh.sign_payload("right", ts, body)
        assert wh.verify_signature("wrong", sig, body) is False

    @pytest.mark.invariant
    def test_verify_rejects_tampered_body(self):
        ts = int(time.time())
        sig = wh.sign_payload("k", ts, '{"a":1}')
        # Same signature, tampered body
        assert wh.verify_signature("k", sig, '{"a":2}') is False

    @pytest.mark.invariant
    def test_verify_rejects_old_timestamp(self):
        """INVARIANT: a signature older than the tolerance must be
        rejected, even if otherwise valid. Defeats replay attacks."""
        old_ts = int(time.time()) - 600  # 10 min ago, > default 300s
        body = '{"a":1}'
        sig = wh.sign_payload("k", old_ts, body)
        assert wh.verify_signature("k", sig, body, tolerance_sec=300) is False

    def test_verify_rejects_malformed_header(self):
        body = '{"a":1}'
        assert wh.verify_signature("k", "garbage", body) is False
        assert wh.verify_signature("k", "v1=abc", body) is False  # missing t


# ---------------------------------------------------------------------------
# Delivery store
# ---------------------------------------------------------------------------


class TestDeliveryStore:
    def test_enqueue_creates_pending_row(self):
        store = wh.WebhookDeliveryStore(db_path=None)
        i = store.enqueue(
            audit_event_id="aid-1", event="SEND_SUCCESS",
            url="https://example.com/wh", payload='{"k":1}',
        )
        rec = store.get(i)
        assert rec is not None
        assert rec.status == "pending"
        assert rec.attempts == 0
        assert rec.url == "https://example.com/wh"

    def test_take_due_returns_only_due_pending(self):
        store = wh.WebhookDeliveryStore(db_path=None)
        i1 = store.enqueue(
            audit_event_id="a", event="SEND_SUCCESS",
            url="u1", payload="{}",
        )
        i2 = store.enqueue(
            audit_event_id="b", event="SEND_SUCCESS",
            url="u2", payload="{}",
        )
        # Both initially have next_attempt_at = enqueue time, so both due.
        due = store.take_due(limit=10)
        assert {d.id for d in due} == {i1, i2}

    def test_force_retry_resets_giveup_to_pending(self):
        store = wh.WebhookDeliveryStore(db_path=None)
        i = store.enqueue(
            audit_event_id="x", event="SEND_SUCCESS",
            url="u", payload="{}",
        )
        store.update_outcome(
            i, status="giving_up", attempts=5,
            next_attempt_at=0.0, last_response_at=time.time(),
            last_response_code=500, last_error="server down",
        )
        assert store.force_retry(i) is True
        rec = store.get(i)
        assert rec.status == "pending"
        # Attempts cumulative — not reset
        assert rec.attempts == 5

    def test_force_retry_refuses_delivered(self):
        store = wh.WebhookDeliveryStore(db_path=None)
        i = store.enqueue(
            audit_event_id="x", event="SEND_SUCCESS",
            url="u", payload="{}",
        )
        store.update_outcome(
            i, status="delivered", attempts=1, next_attempt_at=0.0,
            last_response_at=time.time(), last_response_code=200,
            last_error=None,
        )
        assert store.force_retry(i) is False

    def test_persists_across_restart(self, tmp_path):
        db = tmp_path / "wh.db"
        s1 = wh.WebhookDeliveryStore(db_path=str(db))
        i = s1.enqueue(
            audit_event_id="p", event="SEND_SUCCESS",
            url="https://u", payload='{"a":1}',
        )
        s1.close()
        s2 = wh.WebhookDeliveryStore(db_path=str(db))
        rec = s2.get(i)
        assert rec is not None
        assert rec.event == "SEND_SUCCESS"
        s2.close()


# ---------------------------------------------------------------------------
# Worker — retry / giveup
# ---------------------------------------------------------------------------


class TestWorkerRetry:
    def _make(self, store, secret="testsecret",
              backoff=(0.0, 0.1, 0.1, 0.1, 0.1)):
        return wh.WebhookWorker(
            store, signing_secret=secret,
            backoff_schedule=backoff,
            timeout_sec=2.0, interval_sec=10.0,
        )

    @responses.activate
    def test_2xx_marks_delivered(self):
        url = "https://example.com/ok"
        responses.add(responses.POST, url, status=200, body="ok")
        store = wh.WebhookDeliveryStore(db_path=None)
        i = store.enqueue(
            audit_event_id="a", event="SEND_SUCCESS",
            url=url, payload='{"event":"SEND_SUCCESS"}',
        )
        worker = self._make(store)
        # Run one delivery manually
        worker._deliver_one(store.get(i))
        rec = store.get(i)
        assert rec.status == "delivered"
        assert rec.last_response_code == 200

    @responses.activate
    def test_5xx_schedules_retry(self):
        url = "https://example.com/down"
        responses.add(responses.POST, url, status=503, body="oops")
        store = wh.WebhookDeliveryStore(db_path=None)
        i = store.enqueue(
            audit_event_id="b", event="SEND_SUCCESS",
            url=url, payload="{}",
        )
        worker = self._make(store)
        worker._deliver_one(store.get(i))
        rec = store.get(i)
        assert rec.status == "pending"   # back to pending for retry
        assert rec.attempts == 1
        assert rec.last_response_code == 503
        assert rec.next_attempt_at >= time.time()  # scheduled in future

    @responses.activate
    def test_exhausting_retries_marks_giveup(self):
        url = "https://example.com/permanent-fail"
        # 5 failed responses (matches 5-step backoff in _make)
        for _ in range(5):
            responses.add(responses.POST, url, status=500, body="fail")
        store = wh.WebhookDeliveryStore(db_path=None)
        i = store.enqueue(
            audit_event_id="c", event="SEND_SUCCESS",
            url=url, payload="{}",
        )
        worker = self._make(store)
        # Force the worker through the schedule
        for _ in range(5):
            rec = store.get(i)
            if rec.status not in ("pending",):
                break
            worker._deliver_one(rec)
        rec = store.get(i)
        assert rec.status == "giving_up"
        assert rec.attempts == 5

    @responses.activate
    def test_giveup_writes_audit_event(self):
        """A WEBHOOK_GIVEUP event lands in the audit log so the operator
        gets a clear signal that a receiver is permanently failing."""
        url = "https://example.com/forever-down"
        for _ in range(5):
            responses.add(responses.POST, url, status=500)
        store = wh.WebhookDeliveryStore(db_path=None)
        i = store.enqueue(
            audit_event_id="d", event="SEND_SUCCESS",
            url=url, payload="{}",
        )
        worker = self._make(store)
        with patch("skr_crypto.server.webhooks.audit") as mock_audit:
            for _ in range(5):
                rec = store.get(i)
                if rec.status != "pending":
                    break
                worker._deliver_one(rec)
            # Audit was called with WEBHOOK_GIVEUP event
            # Look for WEBHOOK_GIVEUP in any call to audit.record
            text_dump = " ".join(str(c) for c in mock_audit.record.call_args_list)
            assert "WEBHOOK_GIVEUP" in text_dump


# ---------------------------------------------------------------------------
# Money-path invariants
# ---------------------------------------------------------------------------


class TestWebhookHotPathInvariants:
    @pytest.mark.invariant
    def test_send_does_not_block_on_webhook(
        self, client, auth_headers, mock_tron, monkeypatch,
    ):
        """INVARIANT: a slow webhook receiver MUST NOT delay /send.
        We simulate by configuring webhooks with a deliberately slow
        URL — the /send call should still complete in the normal time
        window, because the worker is a daemon thread."""
        # Initialise webhooks pointing at a URL that would hang. We
        # don't actually start the worker — what we test is that the
        # ENQUEUE step (which is on the request path) doesn't block.
        from skr_crypto.server import webhooks as wh_mod
        original_store = wh_mod.store
        original_filter = wh_mod._event_filter
        original_urls = wh_mod._target_urls
        wh_mod.store = wh_mod.WebhookDeliveryStore(db_path=None)
        wh_mod._event_filter = ("SEND_SUCCESS",)
        wh_mod._target_urls = ("http://nothing.invalid/wh",)
        try:
            t0 = time.time()
            resp = client.post("/api/v1/send", headers=auth_headers, json={
                "to_address": "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL",
                "amount": "1.0",
                "idempotency_key": "wh-hot-path",
            })
            elapsed = time.time() - t0
            assert resp.status_code == 200, resp.text
            # Generous threshold — should complete in <2s; we assert <5s
            # to be robust on slow CI runners. The whole point: ENQUEUE
            # is just a SQLite INSERT, no HTTP.
            assert elapsed < 5.0, f"/send took {elapsed:.2f}s — webhook blocked it"
        finally:
            wh_mod.store = original_store
            wh_mod._event_filter = original_filter
            wh_mod._target_urls = original_urls


class TestEventFilter:
    def test_audit_event_outside_filter_does_not_enqueue(self):
        from skr_crypto.server import webhooks as wh_mod
        wh_mod.store = wh_mod.WebhookDeliveryStore(db_path=None)
        wh_mod._event_filter = ("SEND_SUCCESS",)  # only this
        wh_mod._target_urls = ("https://u",)
        try:
            wh_mod.enqueue_event_for_delivery(
                "aid-x", "STARTUP_CHECK", {"event": "STARTUP_CHECK"},
            )
            # Nothing enqueued
            assert wh_mod.store.list_recent() == []
        finally:
            wh_mod.store = None
            wh_mod._target_urls = ()
            wh_mod._event_filter = ()

    def test_audit_event_in_filter_enqueues_one_per_url(self):
        from skr_crypto.server import webhooks as wh_mod
        wh_mod.store = wh_mod.WebhookDeliveryStore(db_path=None)
        wh_mod._event_filter = ("SEND_SUCCESS",)
        wh_mod._target_urls = ("https://u1", "https://u2")
        try:
            wh_mod.enqueue_event_for_delivery(
                "aid-y", "SEND_SUCCESS", {"event": "SEND_SUCCESS", "amount": "1"},
            )
            rows = wh_mod.store.list_recent()
            urls = sorted(d.url for d in rows)
            assert urls == ["https://u1", "https://u2"]
        finally:
            wh_mod.store = None
            wh_mod._target_urls = ()
            wh_mod._event_filter = ()
