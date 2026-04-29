# Architecture Decision Records

This directory holds the architecture decision records (ADRs) for
`skr-crypto`. Each ADR records a single architectural choice — what
was decided, the context that made it the right call at the time, and
the alternatives that were considered and rejected. They are the long
answer to "why is it like this?" for the parts of the system that
warrant one.

## Index

| # | Title | Summary |
|---|---|---|
| [0001](0001-sync-only-architecture.md) | Sync-only architecture, no asyncio on the money path | Internal logic is synchronous; async exists only at the FastAPI boundary, never past the route handler. |
| [0002](0002-pluggable-key-providers.md) | Pluggable secret backends via KeyProvider abstraction | Four `KeyProvider` backends (`env`, `file`, `keychain`, `1password`) selected by `KEY_PROVIDER` env var, validated at boot. |
| [0003](0003-msk-day-reconciliation.md) | MSK-day window for startup reconciliation | The boot self-check window is `[midnight MSK today, now]`, hardcoded; cross-midnight checks are a manual `skr-crypto reconcile` invocation. |
| [0004](0004-audit-hard-error.md) | Audit write failure is a hard error, not a warning | A failed `_write_durable` raises `AuditWriteError`, releases the idempotency slot, and returns `HTTP 500` to the client; the broadcast does not happen. |

## Format

Each ADR has the same shape — `## Status`, `## Context`, `## Decision`,
`## Consequences`, `## Alternatives considered`, and a `## Related`
block at the end. The status line carries an `Accepted` verdict and
the date the decision was locked in; the date matches the version
that introduced the decision (typically a `1.0.0` cut or a subsequent
release that documented existing behaviour).

The voice is the same as the rest of the documentation in this repo:
direct, opinionated, prose-heavy where the "why" matters, terse where
it doesn't. The point is not to produce a corporate compliance
artefact but to give a future maintainer — possibly the original
author who has forgotten — a fast answer to "why isn't this done the
way the textbook would have done it?"

## ADRs are immutable

A decision, once recorded and accepted, **does not get edited**. If a
later change makes an old ADR wrong, the old ADR stays exactly as it
was and a new ADR supersedes it. The new ADR's title says so
explicitly — for example, `0005-supersedes-0004.md` would document
the new decision and its `Status` block would link back to 0004 with
a `Supersedes ADR 0004` note. The old ADR's status is updated only to
add a `Superseded by ADR 0005` line; nothing else changes.

The reason for the immutability is the same reason the audit log is
immutable: edits make the trail unreliable. A future operator reading
0004 needs to know what we believed in April 2026, even if we believe
something different in 2027. Editing 0004 in place would erase the
2026 belief, and the link from 0005 would point at empty space.

## How to add a new ADR

1. **Copy the most recent ADR as a template.** The structure is
   uniform across the set, and the easiest way to keep it that way is
   to start from a known-good copy.
2. **Increment the number.** ADR numbers are sequential, four digits,
   never reused. The next one is `0005`.
3. **Pick a short kebab-case slug** that names the decision, not the
   problem. `0005-postgres-idempotency-store.md` is a slug; `0005-fix-database.md` is not.
4. **Write the ADR.** Aim for 80-130 lines. Be explicit about what is
   in scope and what is not. The Alternatives section is not a
   formality — if there were no alternatives, the decision did not
   need an ADR.
5. **Link it from this README.** Add a row to the index table above
   with the title and a one-line summary.
6. **Get the ADR reviewed.** Open a PR. The review covers the same
   surface as a code review: is the reasoning honest, is the
   alternatives section complete, is anything missing from the
   `Related` block. Merge once approved.

If the new ADR supersedes an existing one, update the existing one's
`Status` block (a single `Superseded by ADR NNNN` line, nothing else)
and reference it from the new ADR's `Status` block as well. Do not
edit the body of the superseded ADR.
