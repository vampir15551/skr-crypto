"""End-to-end concurrency tests through the FastAPI router.

These are intentionally combined with the more thorough thread-level tests in
tests/test_idempotency.py::TestIdempotencyConcurrency. The tests here verify
that the route layer correctly wires reserve/commit/release into idempotency,
even though TestClient itself serializes requests under the hood — so the
guarantee being checked here is really "does the integration produce the
right HTTP responses", not "is reservation race-safe" (that is covered
directly on the store).
"""
from __future__ import annotations

import threading
import time

VALID_ADDRESS = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"


class TestConcurrentSend:
    def test_two_sequential_requests_same_key_one_broadcast(
        self, client, auth_headers, mock_tron,
    ):
        """Sequential duplicate request must return cached txid, not re-broadcast."""
        call_count = {"n": 0}

        def counting_send(to, amount, **kwargs):
            call_count["n"] += 1
            return "the-txid"

        mock_tron.send_usdt = counting_send

        payload = {
            "to_address": VALID_ADDRESS,
            "amount": "10",
            "idempotency_key": "seq-key",
        }

        r1 = client.post("/api/v1/send", json=payload, headers=auth_headers)
        r2 = client.post("/api/v1/send", json=payload, headers=auth_headers)

        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["status"] == "broadcast"
        assert r2.json()["status"] == "duplicate"
        assert r1.json()["txid"] == r2.json()["txid"] == "the-txid"
        assert call_count["n"] == 1, f"send_usdt called {call_count['n']} times"

    def test_concurrent_threads_same_key_no_double_send(
        self, client, auth_headers, mock_tron,
    ):
        """Even if TestClient mostly serializes, no thread should ever observe
        a state where send_usdt was called twice for the same key.

        Real concurrency on the underlying store is exercised in
        tests/test_idempotency.py::TestIdempotencyConcurrency.
        """
        call_count = {"n": 0}
        lock = threading.Lock()

        def counting_send(to, amount, **kwargs):
            # Hold for a moment so a second request would have a chance to
            # reach reserve() while we're still holding it.
            time.sleep(0.05)
            with lock:
                call_count["n"] += 1
            return "concurrent-txid"

        mock_tron.send_usdt = counting_send

        payload = {
            "to_address": VALID_ADDRESS,
            "amount": "10",
            "idempotency_key": "concurrent-test-key",
        }

        results: list = [None, None]
        errors: list = []

        def do_send(idx):
            try:
                results[idx] = client.post(
                    "/api/v1/send", json=payload, headers=auth_headers,
                )
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=do_send, args=(0,))
        t2 = threading.Thread(target=do_send, args=(1,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Threads raised: {errors}"
        assert results[0] is not None and results[1] is not None
        assert results[0].status_code == 200 and results[1].status_code == 200

        statuses = sorted([results[0].json()["status"], results[1].json()["status"]])
        # Either both serialized as broadcast→duplicate, or both saw the same
        # already-committed txid and returned duplicate. Never two broadcasts.
        assert statuses in (["broadcast", "duplicate"], ["duplicate", "duplicate"])

        txids = {results[0].json()["txid"], results[1].json()["txid"]}
        assert txids == {"concurrent-txid"}

        assert call_count["n"] == 1, (
            f"send_usdt called {call_count['n']} times — double spend risk!"
        )

    def test_concurrent_different_keys_both_execute(self, client, auth_headers, mock_tron):
        """Different idempotency keys should each produce their own tx."""
        call_count = {"n": 0}
        lock = threading.Lock()

        def counting_send(to, amount, **kwargs):
            with lock:
                call_count["n"] += 1
                n = call_count["n"]
            return f"txid-{n}"

        mock_tron.send_usdt = counting_send

        results: list = [None, None]

        def do_send(idx, key):
            results[idx] = client.post(
                "/api/v1/send",
                json={
                    "to_address": VALID_ADDRESS,
                    "amount": "5",
                    "idempotency_key": key,
                },
                headers=auth_headers,
            )

        t1 = threading.Thread(target=do_send, args=(0, "key-a"))
        t2 = threading.Thread(target=do_send, args=(1, "key-b"))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert results[0].status_code == 200
        assert results[1].status_code == 200
        assert results[0].json()["status"] == "broadcast"
        assert results[1].json()["status"] == "broadcast"
        assert call_count["n"] == 2
