# Runbook: audit log recovery

> **Symptom.** Service refuses to start, or `skr-crypto audit` shows malformed JSON lines, or `_write_durable` raises on every request.
> **Cause.** `audit.log` is corrupt, truncated, or unreadable — partial write from a power loss, filesystem damage, accidental edit.
> **TL;DR fix.** Stop the service, snapshot the bad file, find and trim the bad tail, restart, and run `skr-crypto reconcile` to verify against the chain.

**This is a SECURITY-relevant operation.** The audit log is the
system of record (see [`SECURITY.md`](../SECURITY.md) and
[ADR 0004](../docs/adr/0004-audit-hard-error.md)). Any time you
modify the audit file, you have to log the recovery in an
out-of-band channel — your team's incident tracker, a private
gist, an email to yourself with a date — because the audit itself
was compromised and cannot vouch for what just happened.

## 1. Detect

Two failure shapes show up:

- **Boot failure.** The service fails its startup self-check or
  errors out on the first audit write:
  ```
  [BOOT] STARTUP_CHECK error: malformed audit row at line N
  [SEND] AuditWriteError: file appears corrupt
  ```
- **Read-side breakage.** `skr-crypto audit` parses up to a bad
  line and then prints:
  ```
  Error: malformed audit row at line N: <raw bytes>
  ```

Either is enough to confirm. If you only see one bad row, the rest
of the file is probably fine and you only need to surgically
remove that line.

## 2. Stop the service and snapshot

Before touching the file, lock it. A live writer (or a respawning
supervisor) will undo any edit you make — disable systemd /
supervisord respawn first if relevant.

```bash
skr-crypto stop
pgrep -f skr-crypto-server                  # expect: nothing
cp "$AUDIT_LOG_FILE" "$AUDIT_LOG_FILE.broken.$(date +%Y%m%d-%H%M%S)"
ls -lh "$AUDIT_LOG_FILE"*
```

Keep the `.broken` snapshot for at least 30 days — longer if a
compliance requirement applies. The recovery modifies the audit
log; if it later turns out the corruption was somewhere else and
the audit was fine, you need the original to roll back.

## 4. Find the bad line

The audit file is JSON Lines: one record per line. A bad line is
one that does not parse as JSON.

```bash
tail -n 20 "$AUDIT_LOG_FILE"
python3 -c '
import json
with open("'"$AUDIT_LOG_FILE"'") as f:
    for n, line in enumerate(f, 1):
        try: json.loads(line)
        except Exception as e: print(f"line {n}: {e}: {line[:80]!r}")
'
```

The common shape is a partial last line — a crash during
`os.write` left half a record. The fix is to truncate. A bad line
**in the middle** of the file is different: it means a write
landed out of order, which should not happen with the
`fsync`-per-record discipline. Capture the file state, escalate,
and do **not** truncate without understanding what happened.

## 5. Truncate the bad tail (most common case)

If the only bad line is the last line of the file:

```bash
# Replace N with the line number BEFORE the bad one.
head -n <N> "$AUDIT_LOG_FILE" > "$AUDIT_LOG_FILE.fixed"
mv "$AUDIT_LOG_FILE.fixed" "$AUDIT_LOG_FILE"

# Confirm the new tail parses.
python3 -c '
import json
with open("'"$AUDIT_LOG_FILE"'") as f:
    for n, line in enumerate(f, 1):
        json.loads(line)
print("ok")
'
```

This drops the partial record. The service will discover any
unfinished business via the idempotency reconciliation pass on
boot — slots that were `PENDING` when the crash happened become
`UNRESOLVED`, and the operator handles them via
[`unresolved-key.md`](unresolved-key.md).

## 6. Rebuild from on-chain history (last resort)

If the file is unsalvageable — multiple scattered bad lines, or
deleted entirely — you can reconstruct an approximation by walking
the treasury's tronscan history. **This is lossy:** it knows about
successful broadcasts but not rejections, energy-gate refusals,
key loads, or reconciliation outcomes.

```bash
curl -sS "https://api.trongrid.io/v1/accounts/<TREASURY>/transactions/trc20?limit=200" \
    > /tmp/onchain.json
```

For each USDT outbound transfer, synthesize a `SEND_SUCCESS` row
with `reconstructed: true` so it's distinguishable from native
records. Write them into a fresh `audit.log` in chronological
order. **Document the reconstruction** in the out-of-band channel
below — anyone reading the audit later needs to know which section
is reconstructed and which is native.

The CLI does not automate this on purpose: it should be a
deliberate operation, not a keystroke.

## 7. Restart and reconcile

```bash
skr-crypto start
# watch for: [BOOT] Audit file ok ... [BOOT] STARTUP_CHECK ok ...
skr-crypto reconcile
```

If the boot self-check warns about anything — non-`SUCCESS`
receipt codes, duplicate recipients — chase those individually
before calling recovery complete. A clean `reconcile` run with no
mismatches means the post-recovery state matches the chain.

## 8. Out-of-band logging (mandatory)

Before closing the incident, write down somewhere that is **not**
the audit log itself: when corruption was detected, the snapshot
filename, what you changed (truncated tail / removed lines N-M /
rebuilt from on-chain), whether `skr-crypto reconcile` came back
clean, and who performed the recovery. This is the
audit-of-the-audit; the audit log cannot vouch for an operation
that modified it.

## See also

- [`OPERATIONS.md`](../OPERATIONS.md) — operations index
- [`SECURITY.md`](../SECURITY.md) — threat model around audit
  integrity
- [ADR 0004](../docs/adr/0004-audit-hard-error.md) — why the
  service refuses to broadcast on audit write failure
- [`unresolved-key.md`](unresolved-key.md) — handling slots left
  `PENDING` by a crash that corrupted the audit
