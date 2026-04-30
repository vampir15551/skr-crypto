# 0007 — Engineering safety practices for the money path

## Status

Accepted. 2026-04-30. Introduced in 1.5.0.

## Context

Six previous ADRs decided **what** the system does. This one decides **how
we keep it from breaking** as features arrive. As of 1.5.0 the project is
moving from "single-author, single-deploy, infrequent change" to
"actively developed, multi-feature roadmap". That transition is the
classic moment money-handling code regresses — a feature lands, a test
covers the feature but not the invariant the feature interacts with, an
operator hits the regression in production six months later.

The defenses we already have (sync-only mainline, audit hard-error,
idempotency state machine, immutable ADRs, signed tags, ~390 tests) are
necessary but not sufficient. They tell us "this is correct **today**";
they don't tell us "this stays correct **after the next 50 PRs**".

This ADR is the rule book for staying correct over time.

## Decision

Adopt the following engineering practices, all enforced in CI where
possible, documented where not:

### 1. Invariant tests are explicit, marked, and gated

The test suite contains tests of two kinds:

- **Behaviour tests** — "this feature does what its docstring claims".
- **Invariant tests** — "if this assertion ever flips, money is at
  risk". Examples already in the suite: `test_audit_write_failure_returns_500`,
  `test_empty_txid_raises`, `test_orphaned_pending_promoted_to_unknown`,
  `test_sanctioned_address_blocked`.

The two are not equal. Invariant tests are marked with
`@pytest.mark.invariant` (registered in `pyproject.toml`). A CI gate
fails any PR that modifies an invariant test **without** also adding
a new ADR superseding the relevant prior ADR.

This means: you cannot land a PR that loosens an invariant by
"updating the assertion". You must explicitly write an ADR that says
"the old invariant is dead, here is the new one, and here is the
justification". The ADR review is the cooling-off period.

### 2. Adversary tests cover every threat in SECURITY.md

For every adversary class documented in `docs/security.md`, there is a
corresponding test in `tests/test_threat_model.py`. A new threat class
**requires** a new test. A test cannot be deleted without an ADR.

This is the closing of an otherwise-common gap: the security doc says
"we defend X" and the code maybe-defends X but the test suite never
exercises the defence directly.

### 3. Wire-format golden compatibility

Each major-versioned API response (v1.0, v1.1, v1.2, …) has a JSON
golden snapshot in `tests/fixtures/api_v1_golden/`. A regression test
asserts that every golden's required-fields-from-its-version are still
present in the current implementation's response.

Adding a field is fine. Removing a field, renaming a field, or
changing a field's semantics is NOT fine without:

- An entry in `CHANGELOG.md` under "BREAKING".
- An ADR explaining the trade-off.
- A new major version bump.

### 4. Property-based tests on critical paths

`tests/test_*_property.py` modules use Hypothesis to fuzz:

- Idempotency state transitions (no two distinct commits for one key
  under any concurrent reserve sequence).
- TRON address validation (random byte sequences never crash).
- Encrypted keystore parse (random byte flips → `KeystoreCorrupt` or
  `WrongPassphrase`, never silent corruption or crash).

Property-based tests run on every CI build. Hypothesis examples are
pinned via `.hypothesis/examples/` so a regression triggers
deterministically.

### 5. Mutation testing on a monthly cadence

A scheduled GHA workflow runs `mutmut` against the money-path modules
once per month. It opens an issue if the survival rate exceeds the
threshold. Mutation testing ≠ coverage; it measures whether tests
*notice* logic changes.

### 6. CHANGELOG entry is mandatory

A CI gate fails any PR that modifies `skr_crypto/` without also
modifying `CHANGELOG.md` under `[Unreleased]`, unless the PR carries
a `skip-changelog` label (reserved for non-functional refactors).

### 7. CODEOWNERS protect the money path

The following files require review from a designated owner:

- `skr_crypto/server/wallet.py`
- `skr_crypto/server/wallet_pool.py`
- `skr_crypto/server/idempotency.py`
- `skr_crypto/server/audit.py`
- `skr_crypto/server/encrypted_keystore.py`
- `skr_crypto/server/risk.py`
- `skr_crypto/server/sanctions.py`
- `skr_crypto/server/key_providers.py`
- `skr_crypto/server/security.py`
- `skr_crypto/server/tokens.py` (added 1.5.0)
- `tests/conftest.py`

Branch protection on `main` requires CODEOWNERS approval before merge.
While the project has a single committer, this is a self-discipline gate
(a 24-hour cooling period between writing and self-approving). When the
project gains a second committer, the rule becomes a real two-eyes check.

### 8. Signed commits and signed tags

Every commit is signed (configured locally; verified by GitHub).
Every release tag is ed25519-signed. CI verifies the tag signature
before building a release wheel. An unsigned commit on `main` is an
incident, not a normal occurrence.

### 9. Canary deploy after each release

A `canary.yml` workflow (manually triggered after each release) spins
up a container with the freshly-built wheel, points at a canary VPS,
and runs five real $1 USDT mainnet transfers to known-clean addresses.
The audit log is verified, retry-with-same-key returns duplicate, and
the on-chain receipts are checked via a node distinct from the
service's TronGrid endpoint.

The release is "production-ready" only after the canary passes.

### 10. Dependency lockdown + reproducible builds

`requirements-lock.txt` is regenerated weekly via a scheduled workflow.
`release.yml` builds the wheel with `SOURCE_DATE_EPOCH` set to the
commit timestamp; the resulting `.whl` is bit-for-bit reproducible
from the same git revision.

### 11. PR checklist (CONTRIBUTING.md)

Every PR must answer:

- What does this change?
- Does it touch the money path? (CODEOWNERS will tell you.)
- Does it modify any `@pytest.mark.invariant` test? If yes — link the
  superseding ADR.
- Does it change a documented HTTP response shape? If yes — link the
  golden-file update.
- New dep added? Why? `pip-audit` clean?

The template is in `.github/PULL_REQUEST_TEMPLATE.md`. CI fails on a
PR with the template's checkboxes still unchecked.

## Consequences

**Code velocity goes down by ~20%.** PRs that previously merged in
hours now take a day, because of the cooling-off and the checklist.
This is the explicit trade-off — a money-mover doesn't optimise for
velocity past the safety floor.

**Onboarding is heavier.** A new contributor has to read seven ADRs,
the threat model, and the contribution guide before they understand
what's "in scope" for changes. We accept this cost — the alternative
is a system that can't be maintained safely by anyone but the
original author.

**Most "easy" PRs (typo fixes, doc updates, dependency bumps) bypass
most of this** via the `skip-changelog` label and the absence of
money-path file changes. The full machinery only fires when it
should.

**Catastrophic regressions become much harder to ship.** Every
mechanism above maps to a specific past-or-imagined failure:

- Invariant gate → "we softened audit to a warning"
- Threat-model tests → "we added IP allow-list logic that was never tested against an attacker"
- Wire-format golden → "we renamed `from_address` and broke every existing caller"
- Property-based on idempotency → "two threads with the same key produced two distinct commits in a race we hadn't thought about"
- Canary deploy → "the release passed CI but our env vars in prod weren't what CI tested"

**ADR overhead is real.** Every architectural change requires a doc.
Sometimes you write a 200-line ADR for a 10-line code change. We
accept this — the ADR is the compounded value, not the code change.

## Alternatives considered

- **Do nothing — trust the existing ~390 tests.** Tempting at single-author
  scale. Stops working at 2+ committers, or after 12 months when the
  original author has forgotten the context. Not viable as a roadmap-driven
  project.
- **Outsource to bug-bounty platforms.** Useful, but reactive — finds
  bugs that already shipped. Doesn't prevent them.
- **Type-check everything with mypy --strict.** Adds value but doesn't
  catch the failure modes we actually have (audit gates, race conditions,
  on-chain semantics). Worth doing eventually, not the right primary lever.
- **Move to formal verification.** Unrealistic for a Python codebase with
  external dependencies (tronpy, requests, sqlite). The cost-benefit
  curve breaks down past property-based testing.
- **A "money path freeze" (no changes ever to the listed files).**
  Impossible — bug fixes happen. Better to gate changes than to
  pretend they won't happen.

## Related

- [ADR 0001](0001-sync-only-architecture.md) — the original "what".
- [ADR 0004](0004-audit-hard-error.md) — the kind of decision this ADR
  is meant to keep enforced.
- `CONTRIBUTING.md` — the operational form of this ADR.
- `.github/CODEOWNERS` — the enforcement file.
- `tests/test_threat_model.py` — the test version of `docs/security.md`.
