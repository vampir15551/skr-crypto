# ADR 0003: MSK-day window for startup reconciliation

## Status

Accepted — 2026-04-29. Locked in for `skr-crypto` 1.x. The window
boundary is hardcoded; making it configurable is an explicit non-goal
until we have a second operator in a different timezone.

## Context

Every server boot calls `startup_check.run_startup_check()` before the
HTTP listener opens. The check walks the durable audit log, picks out
every `SEND_SUCCESS` record in a bounded window, and re-verifies each
txid on-chain via TronGrid `get_transaction_info`. It also groups
successes by `to_address` and warns if the same recipient appears more
than once in the window.

The point of the check is to give the operator a clean answer to a
specific question on every boot: *did everything we thought we sent
today actually land, and did we accidentally pay anyone twice?* That
question only makes sense relative to a window. Pick the wrong window
and the answer is either noisy (we surface yesterday's already-handled
transactions) or unsafe (we miss something that needed attention).

Three obvious windows exist: a fixed wall-clock day in some timezone,
a rolling 24-hour window, or a configurable period. This ADR records
which one we picked and why.

## Decision

**The window is `[midnight MSK today, now]`, where MSK is UTC+3 with no
DST.** It is hardcoded in `startup_check.py`. There is no env var to
override it, and the function does not accept a window argument from
its caller.

The boundary is computed once per invocation: take the current UTC
time, add three hours, truncate to the start of the day, subtract
three hours back. The result is a UTC timestamp that corresponds to
00:00 Moscow time on the operator's current local day.

MSK was picked deliberately. The operator (us) is in Moscow. "Today's
payouts" is an operator-meaningful phrase exactly because it lines up
with the operator's working day; a mismatch between the reconciliation
window and the calendar the operator reads off the wall is
operationally confusing in a way that a payment service cannot afford
to be confusing about.

The boundary is hardcoded because configurability would force every
deployment to think about what timezone "today" means in their context,
and there is currently exactly one deployment. YAGNI.

## Consequences

**The boot check is fast and bounded.** Audit volume per day is small
(tens of records on a busy day, single digits on a slow one), and
re-verifying tens of txids against TronGrid completes in a couple of
seconds. The check does not gate normal traffic for long; the listener
opens once it returns.

**Cross-midnight reconciliation is a manual operation.** A boot at
00:30 MSK reconciles the half-hour from midnight to now. Yesterday's
payouts are not in the window. If the operator needs to re-verify
yesterday — for example, after a power loss right before midnight that
left the audit ambiguous — they run `skr-crypto reconcile` with an
explicit date argument. The CLI surfaces this in the
`reconcile --help` text precisely because the boot check is silent
about anything outside today.

**No DST surprises.** Moscow is UTC+3 year-round; we don't have to
think about a 25-hour day in autumn or a non-existent hour in spring.
Treating the offset as a constant is correct, and it keeps the
implementation a one-liner instead of a `zoneinfo` dance.

**Duplicate-recipient warnings are scoped to today.** The grouping by
`to_address` only catches duplicates that happened in the current MSK
day. A duplicate split across a midnight boundary will not surface
from the boot check; it requires the manual reconcile path. We have
not seen this in practice — the idempotency-key format expected by the
client is `<to_address><YYYY-MM-DD>`, which already prevents
same-day-same-wallet at the contract level — but it is worth being
explicit that the boot check is a backstop, not the primary defense.

**Forward-looking caveat.** This ADR assumes a single host. The audit
log is local, the boot check reads it directly off the local
filesystem, and "today's payouts" is a question with a single answer
because there is a single writer. If we ever move to multi-host — even
a hot-cold pair for failover — we will need to revisit this: the audit
becomes a distributed object, the window calculation has to agree
across hosts, and the duplicate-recipient check has to consider
cross-host sends. Adding that is a project, not a flag.

## Alternatives considered

**Rolling 24-hour window.** Rejected. The implementation is trivial —
`now - 24h` — but the result is harder for the operator to reason
about. "Did we double-pay anyone today?" and "did we double-pay anyone
in the last 24 hours?" are different questions with different answers,
and only the first one matches what the operator has in their head
when they read the boot output. A rolling window also has the awkward
property that the same pair of transactions can appear in two
consecutive boot reports, which is more noise to filter.

**UTC midnight.** Rejected for the same reason MSK was picked: it is
not the operator's day. A reconciliation at 03:00 MSK on the morning
of the 27th is reconciling the period since 03:00 MSK on the 27th —
not since the 27th started — which is a confusing thing to print at
the top of a logfile. UTC is the right answer for any system that has
operators in multiple timezones; we don't.

**Configurable timezone.** Rejected as YAGNI. We have one operator and
that operator is in Moscow. Adding a `RECONCILE_TIMEZONE` env var means
adding validation, documentation, doctor checks, test coverage, and a
mental model for what "today" means in a service that might be
deployed in three timezones simultaneously — none of which we have.
When a second operator in a different timezone shows up, this ADR
gets a successor and the env var arrives with it. Until then, the
hardcoded constant is the right amount of code.

## Related

- [`OPERATIONS.md` § Startup self-check](../monitoring.md)
- [`runbooks/audit-recovery.md`](../audit-log.md)
- `skr_crypto/server/startup_check.py`
