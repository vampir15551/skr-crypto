# Audit log

The audit log is the **immutable, durable, machine-readable** record of every financial action the service takes. It is not a debug log. It is not best-effort. If `AUDIT_LOG_FILE` is set and the disk write fails, the service returns 500 to the caller — see [ADR 0004](adr/0004-audit-hard-error.md).

Implementation: `skr_crypto/server/audit.py`.

---

## Record format

Each record is one line of JSON. The file is append-only; nothing is ever rewritten.

```json
{
  "id": "676dde64-9",
  "timestamp": "2026-04-30T07:42:59.748789+00:00",
  "event": "SEND_SUCCESS",
  "wallet": "cold",
  "from_address": "TColdxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "to_address": "TRX9SbJzPbXYK1yw6VaFhCJ7ZKAoGwEPwy",
  "amount": "245.50",
  "asset": "USDT",
  "txid": "abcdef0123456789...",
  "idempotency_key": "invoice-2026-04-30-#741",
  "client_ip": "10.0.0.42",
  "result": "broadcast",
  "details": "elapsed=1.43s risk=low"
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | string | `<process-uuid>-<sequence>`. Uniquely identifies a record across restarts (uuid changes per process). |
| `timestamp` | ISO 8601 with `+00:00` | UTC, microsecond precision. |
| `event` | string | One of the values below. |
| `wallet` | string | Source wallet name (1.4.0+). Empty for non-SEND events. |
| `from_address` | string | Source TRON address. |
| `to_address` | string | Recipient. |
| `amount` | string | Decimal as string to avoid float drift. |
| `asset` | string | Always `"USDT"` in the current build. |
| `txid` | string | On-chain txid; empty for events before broadcast. |
| `idempotency_key` | string | Caller-provided. |
| `client_ip` | string | Direct peer or X-Forwarded-For first hop (if `TRUSTED_PROXIES` matches). |
| `result` | string | Free-form; values per event listed below. |
| `details` | string | Free-form context. Optional — only present when relevant. |

---

## Event types

### `SEND_SUCCESS`

The transaction was successfully broadcast and got a txid.

```json
{
  "event": "SEND_SUCCESS",
  "wallet": "cold",
  "from_address": "T...", "to_address": "T...",
  "amount": "245.50", "txid": "abc...",
  "idempotency_key": "invoice-741",
  "result": "broadcast",
  "details": "elapsed=1.43s risk=low",
  ...
}
```

`details` includes elapsed time and the risk verdict. `risk=skipped` means `RISK_BLOCK_LEVEL=none`.

### `SEND_DUPLICATE`

A retry hit a previously-committed idempotency key. The original txid is returned to the caller.

```json
{
  "event": "SEND_DUPLICATE",
  "wallet": "cold",
  "amount": "245.50", "txid": "abc...",
  "idempotency_key": "invoice-741",
  "result": "duplicate",
  ...
}
```

A high rate of `SEND_DUPLICATE` is a smell — usually a buggy retry loop in the caller.

### `SEND_REJECTED`

A request was refused before broadcasting. `result` carries the reason:

| `result` | Meaning |
|---|---|
| `insufficient_usdt` | wallet's USDT balance < amount |
| `insufficient_trx` | wallet's TRX balance < `MIN_TRX_RESERVE` |
| `energy_too_expensive` | estimated burn exceeds `MAX_ENERGY_BURN_TRX` |
| `risk_too_high` | recipient verdict ≥ `RISK_BLOCK_LEVEL` |

```json
{
  "event": "SEND_REJECTED",
  "wallet": "cold",
  "to_address": "TRsanctioned...",
  "amount": "100",
  "result": "risk_too_high",
  "details": "level=high failed=sanctions",
  ...
}
```

### `SEND_FAILED`

Broadcast was attempted but failed:

| `result` | Meaning |
|---|---|
| `tx_failed` | `wallet.send_usdt` raised — node down, signature rejection, broadcast returned non-success |
| `rpc_failed` | a preflight RPC raised after we'd reserved the idempotency slot |

```json
{
  "event": "SEND_FAILED",
  "result": "tx_failed",
  "details": "broadcast rejected: code=BANDWIDTH_ERROR ...",
  ...
}
```

### `STARTUP_CHECK`

Once per boot. The MSK-day reconciliation (see [ADR 0003](adr/0003-msk-day-reconciliation.md)) writes its summary here:

```json
{
  "event": "STARTUP_CHECK",
  "result": "ok",
  "details": "window_start=2026-04-30T00:00:00+00:00 checked=12 by_status={'SUCCESS': 12} duplicates=0",
  ...
}
```

`result` is `"ok"` if every txid resolved to SUCCESS on-chain and there were no duplicate recipients in the window; `"anomaly"` otherwise. The operator should grep for `STARTUP_CHECK.*anomaly`.

---

## Durability semantics

```python
def _write_durable(line: str) -> None:
    fp = _file_fp
    if fp is None:
        if AUDIT_LOG_FILE:
            raise AuditWriteError(
                f"Durable audit configured ({AUDIT_LOG_FILE}) but file open failed earlier — refusing to write"
            )
        return
    try:
        with _file_lock:
            fp.write(line + "\n")
            fp.flush()
            os.fsync(fp.fileno())
    except Exception as exc:
        audit_log.error("Audit fsync failed: %s — line=%s", exc, line[:200])
        raise AuditWriteError(...) from exc
```

**Per-record `flush()` + `fsync()`.** Slow but correct. A power-loss or kernel panic cannot lose an in-flight record once the function returns.

**Single global lock.** Two requests can't interleave bytes mid-line. JSON-per-line is preserved even under heavy contention.

**Hard-fail on disk error.** `AuditWriteError` propagates to the FastAPI handler, which returns 500 `AUDIT_WRITE_FAILED`. The caller knows the broadcast may or may not have happened — they must reconcile.

If `AUDIT_LOG_FILE` is **unset**, the audit goes to stdout only. Acceptable for dev. Not acceptable for production. The install wizard sets it to `data/audit.log` by default.

---

## Operator playbook

### Tail in real time

```bash
tail -f data/audit.log | jq .
```

Or via the CLI (formatted, paginated, with newest-first option):

```bash
skr-crypto audit            # newest first, paginated
skr-crypto audit --since 1h # last hour only
skr-crypto audit --tail 100 # last 100 records
```

### Find every transfer to a recipient

```bash
grep '"to_address":"TRX9SbJzPbXYK1yw6VaFhCJ7ZKAoGwEPwy"' data/audit.log | jq .
```

### Compute today's spent USDT (MSK)

```bash
# MSK = UTC+3, so "today" starts at UTC midnight - 3 hours
START=$(date -u -v-3H +%Y-%m-%dT00:00:00Z)
grep '"event":"SEND_SUCCESS"' data/audit.log | \
  jq -r 'select(.timestamp >= "'"$START"'") | .amount' | \
  awk '{s+=$1} END {print s}'
```

### Audit-the-audit: every reconciliation

```bash
grep '"event":"STARTUP_CHECK"' data/audit.log | jq -r '.details'
```

### Forensics on a specific transfer

```bash
# By txid
grep '"txid":"abc..."' data/audit.log | jq .

# By idempotency key
grep '"idempotency_key":"invoice-741"' data/audit.log | jq .

# Ordered timeline of the request
grep '"idempotency_key":"invoice-741"' data/audit.log | jq -r '"\(.timestamp) \(.event) \(.result)"'
```

---

## Backup strategy

The audit file is **the** financial trail. Treat it like database backups.

- **Daily incremental rsync** to a separate host. Append-only file → rsync is cheap.
- **Quarterly archive** with cryptographic timestamping if you have compliance requirements.
- **Never let it rotate by size or age in-place.** Move-then-touch breaks the durability model. If you must rotate (multi-GB files), do it offline (service stopped, file moved, fresh empty file created with same chmod, service restarted).

The CLI ships `skr-crypto backup` which produces a timestamped tarball of `audit.log` + `idempotency.db` + `keystore.json` + `.env`. See [Backups](commands/backups.md).

---

## What's NOT in the audit

By design:

- **Private keys** or any portion thereof. The audit never sees `priv_key`.
- **Passphrases.** `KEY_PASSPHRASE` is consumed by the keystore loader and never logged.
- **API tokens.** `AUTH_TOKEN` is only ever used in `hmac.compare_digest`; never logged.
- **Risk report bodies in `SEND_SUCCESS`.** Just the level (`risk=low`). The full report would balloon the log without useful retrievability — for the full report, look at `/api/v1/risk/{address}` for the same address (or recompute it).

---

## See also

- [ADR 0004 — Audit hard-error](adr/0004-audit-hard-error.md) — design rationale
- [Idempotency](idempotency.md) — interactions between audit + idempotency state
- [Architecture: storage layers](architecture.md#three-storage-layers-three-durability-stories)
- [Backup CLI commands](commands/backups.md)
