# Runbook: stuck broadcast

> **Symptom.** A client called `POST /api/v1/send` and the connection is hanging — no response, no obvious error.
> **Cause.** Most likely the broadcast call to TronGrid is in retry mode, or it landed but the response never made it back to the client.
> **TL;DR fix.** Confirm the broadcast on tronscan, then rely on the idempotency contract — a retry with the same `Idempotency-Key` returns the cached txid.

The service caps a single broadcast attempt at roughly 50 seconds
worst case (15s timeout × 3 retries with backoff). A request hanging
longer than that is no longer "in flight"; either the client is stuck
on its own side, or the response is sitting on a closed socket.

## 1. Find the in-flight request

First, locate the request in the service. The CLI shows the most
recent in-flight slot, and journalctl has the structured log lines
the request emitted before it hung.

```bash
skr-crypto status
journalctl -u skr-crypto -n 200 --no-pager | grep '\[SEND\]'
```

In the log, look for the most recent `[SEND] Incoming` line **without
a matching** `[SEND] SUCCESS`, `[SEND] FAILED`, or `[SEND] REJECTED`.
That's the request that hung. It will look like this:

```
[SEND] Incoming  key=2026-04-27 to=TX... amount=12.50
[SEND] Estimate  energy=13412 fee_limit_sun=29000000
[SEND] Broadcast  attempt=1
   ...silence...
```

If `[SEND] Broadcast` is the last line for that key, the broadcast
call is the one that's stuck.

## 2. Check whether the broadcast actually went out

Take the `to` address from the log line and search tronscan for it.
Filter by the treasury address in the `from` column.

```bash
open "https://tronscan.org/#/address/<TREASURY_ADDR>/transactions"
```

Look for a transaction in the last few minutes with the right amount
to the right recipient. If you find one, **the broadcast landed** —
the service just hasn't received the HTTP response yet. The retry
ladder is still running.

If you don't see one, give it 30-60 seconds and refresh — TronGrid
sometimes lags tronscan visibility. If after 90 seconds there is
still no transaction, the broadcast did not land and the service
will eventually return `502 broadcast_failed`.

## 3. What the broadcast timeout actually is

The numbers are:

- **Single RPC call timeout:** 15 seconds. Configured in the
  `TronClient` constructor; not user-configurable.
- **Retry count:** up to 3 attempts on connection errors and
  idempotent reads. **`broadcasttransaction` is not retried** —
  see `GUIDELINES.md` § "All RPC calls have explicit timeouts".
- **Backoff:** exponential with jitter, capped at ~10 seconds
  between attempts.
- **Worst case for a balance-or-estimate call:** ~50 seconds
  (15 + 10 + 15 + 10 + ... bounded by the retry count).
- **Worst case for the broadcast call itself:** 15 seconds, then
  it gives up.

A request that has been hanging for more than ~60 seconds is past
all of these and is sitting on something else — a stuck socket on
the client side, a reverse proxy buffer, a NAT timeout. The service
itself has either returned or is about to.

## 4. If the broadcast landed but the client never saw the response

This is the case the idempotency contract was built for. The slot is
`COMMITTED` in the SQLite store, and a retry with the same
`Idempotency-Key` returns the cached response byte-for-byte:

```bash
curl -sS -X POST https://<host>/api/v1/send \
    -H "X-API-Key: $AUTH_TOKEN" \
    -H "Idempotency-Key: <same-key-as-before>" \
    -H "Content-Type: application/json" \
    -d '{"to": "TX...", "amount": "12.50"}'
```

The response body has `replay: true` and the original txid. No second
broadcast happens — the service serves the cached response from
SQLite without touching TronGrid.

Tell the client to retry with the **same idempotency key**. If the
client is using `<to_address><YYYY-MM-DD>` as the key (which is the
expected shape), the retry happens automatically next time the same
payout is requested.

## 5. If the broadcast did not land

If step 2 confirmed there is no on-chain transaction, and the slot
is `UNRESOLVED` in SQLite (you'll see this in the audit), follow
[`unresolved-key.md`](unresolved-key.md). Do not blindly retry — the
idempotency state machine refuses, on purpose.

## 6. Audit events to cross-reference

The audit log shows exactly what the service believes happened.
Filter by the idempotency key:

```bash
grep '"key":"<your-key>"' "$AUDIT_LOG_FILE"
```

You should see one of:

- `SEND_INTENT` followed by `SEND_SUCCESS` — landed, slot `COMMITTED`.
- `SEND_INTENT` with no terminal event — broadcast in flight or
  hung; slot `PENDING` and likely about to become `UNKNOWN`.
- `SEND_INTENT` followed by `SEND_FAILED` — broadcast call raised;
  slot `UNKNOWN`, treat as ambiguous.
- `SEND_REJECTED` — pre-broadcast gate fired; slot `RELEASED`,
  client can retry safely.

The combination of audit + tronscan tells the whole story.

## See also

- [`OPERATIONS.md`](../OPERATIONS.md) — operations index
- [`SECURITY.md`](../SECURITY.md) — threat model around idempotency
- [`unresolved-key.md`](unresolved-key.md) — when the slot is `UNRESOLVED`
- [`rpc-outage.md`](rpc-outage.md) — when TronGrid itself is the problem
