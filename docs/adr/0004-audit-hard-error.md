# ADR 0004: Audit write failure is a hard error, not a warning

## Status

Accepted — 2026-04-29. The behaviour is part of the 1.x stable surface
and is enforced both at the code level (a typed exception) and at the
review level (every PR that touches `_write_durable` is read with this
invariant in mind).

## Context

The durable audit log (`AUDIT_LOG_FILE`) is the system of record for
every state transition that affects on-chain reality. `_write_durable`
serializes a record to a single JSON line, calls `os.write`, and then
calls `os.fsync` on the file descriptor — all three steps before the
caller receives an acknowledgement. The function is used on every
terminal branch of `POST /api/v1/send`: success, rejection, broadcast
failure, key load events, reconciliation outcomes.

A write or fsync can fail for real reasons: the disk filled up, the
volume went read-only, a hardware error returned `EIO`. When that
happens, the question is what `_write_durable` should do, and by
extension what the request should return to the client. This ADR
records which option we picked.

## Decision

**`_write_durable` raises `AuditWriteError` on any failure.** The route
handler catches it, releases the idempotency slot so a retry is safe,
and returns `HTTP 500 {"code": "AUDIT_WRITE_FAILED"}` to the client.
The broadcast does not happen. The slot does not commit. Nothing else
in the request lifecycle runs.

The exception is typed so it cannot be accidentally swallowed by a
broader `except Exception` further up the call stack. A reviewer who
sees a `try/except AuditWriteError: pass` should reject the PR on
sight.

The idempotency slot is released — not committed-with-error, not left
`PENDING` — because the failure happened before broadcast, which means
the client can safely send the same request again with the same key.
Once the operator fixes the disk, the retry proceeds normally.

This refusal-to-broadcast behaviour is the single most important
property of the audit subsystem: the audit row exists before money
moves, or money does not move.

## Consequences

**A failed disk takes the service down in the way the operator
notices.** A read-only volume, a full disk, or a flaky filesystem
results in `HTTP 500` on every `/send` request. The service does not
limp along pretending things are fine; it returns errors loud enough
that the on-call notices in the next minute, not in the next quarterly
audit when a record turns up missing. This is the right failure mode
for a payment service.

**No silent degradation.** There is no in-memory buffer. There is no
fallback file. There is no log-and-continue path. The operator is
guaranteed that for every txid in the audit log there is a `fsync`'d
record on disk, and that the converse — every successful broadcast
the service ever did is in the audit log — also holds.

**Cleaner failure mode than the alternatives.** An operator who reads
`HTTP 500 {"code": "AUDIT_WRITE_FAILED"}` and a stdout line saying
`Audit fsync failed: ENOSPC` knows what to do: free disk, restart,
retry. An operator who discovers a missing audit row days later —
because we silently buffered and then crashed — has a real problem,
and the audit-as-source-of-truth invariant is broken in a way that's
hard to recover from.

**Client retry is safe.** Because the slot is released on
`AuditWriteError`, a client that retries with the same idempotency key
sees a fresh slot, not `UNRESOLVED`. Audit failures are a service-side
problem, and the client should not have to reconcile to recover from
our disk filling up.

**Forward-looking caveat.** This decision assumes a local audit log
on a local disk. If we ever sink the audit to an external store (a
remote append-only log, a journald + remote-collector pipeline, an
object-storage write), the failure semantics may need to change: a
network blip is more transient than a full disk and a short retry
against a remote sink might be appropriate. We are not there yet, and
adding a remote sink is a successor-ADR-and-implementation, not a
config flag.

## Alternatives considered

**Log failure and continue.** Rejected. This is the position that
silently corrupts the compliance trail. A `log.warning("audit failed:
%s", e)` followed by a successful broadcast means there is a real
on-chain transaction the audit log knows nothing about. The service
keeps running, the operator never notices, and weeks later somebody
runs a reconciliation report and finds a hole that nobody can explain
without forensics on stdout. Money services do not get to "best
effort" their audit trail.

**Buffer to in-memory queue.** Rejected on the same principle plus a
race: the buffer is in process memory, the process can crash, and a
crash between the buffer-add and the eventual flush loses the record
without any indication. Even if the buffer is bounded and flushes on a
timer, the window where a record exists in memory but not on disk is a
window where a `kill -9` or a kernel OOM kill silently destroys the
record. The whole point of the durable audit is that it survives
process death.

**Skip audit when disk is full, write when it recovers.** Rejected as
the worst combination: we silently violate the audit invariant *and*
we make recovery harder by leaving a hole the size of however many
requests came in while the disk was full. The "if it's full, don't
audit, audit later" path is the path that lets a duplicate payment go
out and not get caught until somebody reads the audit log a week from
Tuesday.

**Asynchronous audit writer.** Rejected for the same reason
ADR 0001 rejects asyncio everywhere else: the audit-and-commit
critical section has to be a synchronous two-statement sequence in
the same call stack. Putting the audit on a background writer means
the API can return before the audit row is durable, which violates
the audit-first invariant from `GUIDELINES.md`.

## Related

- [`GUIDELINES.md` § Audit-first](../architecture.md)
- [`ARCHITECTURE.md` § Failure modes & recovery](../architecture.md)
- [`runbooks/audit-recovery.md`](../audit-log.md)
- `skr_crypto/server/audit.py`
