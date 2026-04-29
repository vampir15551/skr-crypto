# Runbook: UNRESOLVED idempotency key

> **Symptom.** Client receives `HTTP 409 {"code": "IDEMPOTENCY_UNRESOLVED", "idempotency_key": "...", "created_at": ...}` when retrying a previous request.
> **Cause.** A previous service process crashed mid-broadcast; the slot was promoted from `PENDING` to `UNKNOWN` to `UNRESOLVED` on the next boot.
> **TL;DR fix.** Look up the relevant address on tronscan around `created_at` — if a tx landed, mark the slot `committed` with that txid; if not, delete the slot row. **Never blindly retry an UNRESOLVED key — risk of double-send.**

The `UNRESOLVED` state exists exactly because we refuse to
auto-retry an ambiguous broadcast. The state machine has no path
that unblocks an `UNRESOLVED` key without a human looking at the
chain. That is the whole point. See
[`ARCHITECTURE.md`](../ARCHITECTURE.md#idempotency-state-machine)
and [`GUIDELINES.md`](../GUIDELINES.md) § "Idempotency is a hard
contract".

## The big warning, up front

**Never run `DELETE` or `UPDATE` against the idempotency table on a
hunch.** A wrong call here is a duplicate payment or a missed
reconciliation. The procedure below has explicit verify-on-chain
steps; do not skip them.

## 1. Identify the key and pull context

The 409 response carries `idempotency_key` and `created_at`.
Cross-check against the audit log:

```bash
grep '"key":"<idempotency_key>"' "$AUDIT_LOG_FILE" | jq '.payload.to, .payload.amount, .ts'
```

You should see a `SEND_INTENT` row matching `created_at`, and
**not** a matching `SEND_SUCCESS` or `SEND_FAILED`. If you do see
the latter, the slot is mis-classified — stop and investigate
before touching SQLite. Grab the recipient address and amount from
the payload; you'll need them in the next step.

## 2. Check tronscan

Window of interest: `[created_at, created_at + 90s]`. Broadcasts
that land do so within a couple of minutes; anything older is
either already confirmed in the audit or definitely never landed.

```bash
open "https://tronscan.org/#/address/<TREASURY>/transfers"
```

Look for a TRC-20 USDT transfer from the treasury, to the
recipient from step 1, with the right amount, within ~90s of
`created_at`. Two outcomes matter:

- **Found.** The broadcast landed; only the HTTP response was
  lost. Note the `txid`, go to step 3.
- **Not found** (after waiting an additional 60s for TronGrid lag).
  The broadcast did not happen. Go to step 4.

A genuinely ambiguous case is rare — TRON finalizes fast. If you
find one, escalate; do **not** guess.

## 3. Mark the slot `committed` (transaction landed)

Promote `UNRESOLVED` to `committed` with the on-chain txid, so
subsequent retries replay the cached response.

```bash
sqlite3 "$IDEMPOTENCY_DB_PATH" \
  "UPDATE slots
     SET state = 'committed',
         committed_at = strftime('%s','now') * 1000,
         response_blob = json_object(
           'txid', '<FOUND_TXID>',
           'replay', json('true'),
           'reconciled', json('true')
         )
   WHERE key = '<IDEMPOTENCY_KEY>'
     AND state IN ('unresolved', 'unknown');"

sqlite3 "$IDEMPOTENCY_DB_PATH" \
  "SELECT key, state FROM slots WHERE key = '<IDEMPOTENCY_KEY>';"
# expect: <key>|committed
```

The `AND state IN (...)` guard prevents clobbering a slot that has
somehow already been committed. Then write a `RECONCILE_RESOLVED`
row to the durable audit (this is the point):

```bash
skr-crypto reconcile --resolve <IDEMPOTENCY_KEY> --txid <FOUND_TXID>
```

A client retry with the same key now returns `200 OK` with the
cached txid. No restart required.

## 4. Delete the slot row (transaction did NOT land)

The broadcast did not happen. Remove the slot so a retry sees a
fresh one.

```bash
sqlite3 "$IDEMPOTENCY_DB_PATH" \
  "DELETE FROM slots
    WHERE key = '<IDEMPOTENCY_KEY>'
      AND state = 'unresolved';"

skr-crypto reconcile --discard <IDEMPOTENCY_KEY>
```

The `AND state = 'unresolved'` guard prevents accidentally
deleting an in-flight slot when you have multiple terminals open.

## 5. The "never blindly retry" rule

- Do not run `DELETE` without confirming on-chain that nothing
  landed.
- Do not write a `committed` row with a guessed txid.
- Do not use `--resolve` with a txid you didn't verify belongs to
  this exact recipient + amount + window.

The 409 exists to make you do the work. The service will refuse
to retry on its own forever; that is by design.

## 6. Multi-key incidents

If a power loss left several slots `UNRESOLVED`:

```bash
skr-crypto audit --unresolved
# or:
sqlite3 "$IDEMPOTENCY_DB_PATH" \
  "SELECT key, created_at FROM slots WHERE state = 'unresolved' ORDER BY created_at;"
```

Walk them in `created_at` order. Each gets the full on-chain
verify; do not batch-delete.

## See also

- [`OPERATIONS.md`](../OPERATIONS.md) — operations index
- [`ARCHITECTURE.md`](../ARCHITECTURE.md#idempotency-state-machine)
  — full state-machine description
- [`GUIDELINES.md`](../GUIDELINES.md) § "Idempotency is a hard
  contract"
- [`stuck-broadcast.md`](stuck-broadcast.md) — when the request is
  hanging rather than already dead
- [`audit-recovery.md`](audit-recovery.md) — when the crash also
  corrupted the audit log
