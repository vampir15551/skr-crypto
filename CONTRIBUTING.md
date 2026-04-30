# Contributing to skr-crypto

This is a money-mover. The bar for changes is "would I trust this with
my money in production?". Every rule below maps to a specific past or
imagined failure — none of them are bureaucracy for its own sake.

If a rule doesn't make sense to you, open an issue asking why. Don't
work around it.

The full rationale lives in [ADR 0007 — Engineering safety
practices](docs/adr/0007-engineering-safety-practices.md).

---

## TL;DR for the impatient

```bash
# Setup
make dev                                    # install -e .[dev,docs]

# Before opening a PR
make test && make lint && make docs         # all three must pass
# CHANGELOG.md updated under [Unreleased]   # required (CI gates this)
# .github/PULL_REQUEST_TEMPLATE checkboxes  # required for the merge
```

If you touched any **money-path** file (see [CODEOWNERS](.github/CODEOWNERS)),
also:

- The CODEOWNERS reviewer must approve.
- Any changed `@pytest.mark.invariant` test requires a new ADR superseding
  the relevant prior one.
- Any changed HTTP response shape requires a wire-format golden update.

---

## Money-path files

Files where any change requires extra scrutiny. Listed in
`.github/CODEOWNERS`:

```
skr_crypto/server/wallet.py
skr_crypto/server/wallet_pool.py
skr_crypto/server/idempotency.py
skr_crypto/server/audit.py
skr_crypto/server/encrypted_keystore.py
skr_crypto/server/risk.py
skr_crypto/server/sanctions.py
skr_crypto/server/key_providers.py
skr_crypto/server/security.py
skr_crypto/server/tokens.py
tests/conftest.py
```

Branch protection on `main` requires CODEOWNERS approval before merge.
While the project has a single committer this is a self-discipline gate
(24-hour cooling period between writing and self-approving). When the
project gains a second committer it becomes a real two-eyes check.

---

## The PR checklist

Every PR must answer:

### What does this change?

One sentence in the PR title, plus the long version in the description.
The CHANGELOG entry should match.

### Does it touch the money path?

Run `git diff main --name-only | grep -F -f .github/money-path-files.txt`
(or just look at the file paths). If yes, you need:

- CODEOWNERS approval before merge.
- Awareness that the test suite's invariant gate will check for ADR
  changes if you modified an `@pytest.mark.invariant` test.

### Does it modify any `@pytest.mark.invariant` test?

Run `pytest -m invariant --collect-only` to list them. If your PR
changes the assertion in any of these tests, you need:

- A new ADR superseding the relevant prior one. Number is the next
  available, e.g. `docs/adr/0010-supersedes-0004.md`.
- The new ADR must explain why the invariant changes and what
  replaces it.
- The PR description links to the new ADR.

The invariant-gate workflow refuses to merge without the ADR.

### Does it change a documented HTTP response shape?

If you added/removed/renamed a field on any `/api/v1/*` response:

- Update the golden file in `tests/fixtures/api_v1_golden/` for the
  current major version.
- Run `pytest tests/test_wire_format_compat.py` — it must still pass.
- Update `docs/api-reference.md`.
- If you removed or renamed a field that was present in any prior
  major version's golden, this is a **breaking change** — bump major,
  add to CHANGELOG under "BREAKING".

### Does it add a dependency?

- Justify it in the PR description. We don't add deps casually.
- Run `pip-audit` and confirm no vulnerabilities are reported.
- Pin a sensible range in `pyproject.toml` (e.g. `>=X.Y,<Z.0`).
- Update `requirements-lock.txt` (CI does this on a schedule but
  you should regenerate locally if your PR adds the dep).

### Does it need a CHANGELOG entry?

99% of the time yes. The CHANGELOG-check workflow refuses PRs that
modify `skr_crypto/` without a corresponding `CHANGELOG.md` change
under `[Unreleased]`. Exceptions:

- Pure typo fixes in code comments.
- Pure CI/workflow changes that don't change behaviour.
- Internal refactors with zero observable change.

For these, add the `skip-changelog` label to your PR.

### Did you run the full local validation?

```bash
make test         # all 388+ tests pass
make lint         # ruff clean
make docs         # mkdocs build --strict, no warnings
pip-audit         # no known vulnerabilities
bandit -lll -r skr_crypto       # no high-severity findings
```

CI will run all of these too. Local first saves the round-trip.

---

## Test taxonomy

```bash
# All tests
pytest

# Just the invariants — these are the ones whose failure means
# "money is at risk", not just "feature isn't doing what its
# docstring says".
pytest -m invariant

# Property-based — fuzzes critical paths via Hypothesis
pytest -m property

# Threat-model — one test per adversary class in docs/security.md
pytest tests/test_threat_model.py

# Wire-format compatibility — does v1.0 client still parse current responses?
pytest tests/test_wire_format_compat.py
```

**Invariant tests are sacred.** They are not "tests for code". They
are documented assumptions about how money moves. Each is a contract.
The contract changes only via ADR. See ADR 0007 for the full rule.

---

## Adding a new feature

The happy path:

1. Open an issue describing the feature. Discuss before coding.
2. Decide whether it needs an ADR. Architectural changes (new
   subsystem, new wire format, new authn model, new key provider, new
   storage layer) do. Bug fixes don't. Read the existing ADRs if
   you're unsure.
3. Write the ADR if needed (`docs/adr/00XX-slug.md`). Get it
   reviewed before writing code.
4. Implement in a feature branch.
5. Add behaviour tests. If the feature touches a money-path
   invariant, add or update the invariant test under
   `@pytest.mark.invariant`.
6. Add the threat-model test if the feature changes the security
   surface (new endpoint, new auth path, new exposure of state).
7. Update docs (`docs/`). Strict-mode mkdocs catches broken links.
8. Update `CHANGELOG.md` under `[Unreleased]`.
9. Open the PR with the template filled in.
10. Self-review after 24 hours. Look at the diff with fresh eyes.
11. Merge after CI green + CODEOWNERS approval (which is you, after
    the cooling-off, while the project has one committer).

---

## Adding a new ADR

1. Copy the most recent ADR as a template.
2. Number sequentially. Currently `0009` is the highest; the next
   one is `0010`.
3. Pick a kebab-case slug that names the **decision**, not the
   problem. `0010-receipt-poller.md` is a slug;
   `0010-fix-confirmation-issue.md` is not.
4. Write the standard sections: `Status`, `Context`, `Decision`,
   `Consequences`, `Alternatives considered`, `Related`.
5. Add it to `docs/adr/index.md`'s table.
6. Add it to `mkdocs.yml`'s nav.

If your ADR supersedes an existing one:

- The new ADR's `Status` line says `Supersedes ADR XXXX`.
- Edit the old ADR's `Status` line to add `Superseded by ADR YYYY`.
- **Do not edit anything else in the old ADR.** ADRs are immutable —
  they record what was believed at the time. The new ADR contains
  what's believed now.

---

## Releasing

Follow `RELEASING.md`. Short version:

1. Bump `skr_crypto/version.py` to the new version.
2. Move the `[Unreleased]` CHANGELOG section to a versioned heading.
3. Open `[Unreleased]` again with empty `### Added`/`### Changed`/etc.
4. Commit on `main`.
5. Tag `vX.Y.Z` (signed). Push tag.
6. The `release.yml` workflow builds the wheel + sdist + checksums
   and publishes a GitHub Release.
7. The `docs.yml` workflow rebuilds the docs site.
8. Run the canary deploy workflow manually. If it fails, yank the
   release and investigate.

The release is "production-ready" only after the canary passes.

---

## Cooling-off rule for solo development

While the project has one committer:

- **Open a PR even for your own changes.** Don't push to `main` directly.
- **Wait 24 hours before self-approving.** Re-read the diff with fresh
  eyes; almost always you'll find something.
- **Don't merge during a personal "rush".** If you feel urgency, that's
  a signal to slow down.

This isn't bureaucracy — it's the cheapest possible substitute for
a code review. Once the project has a second committer, the rule
becomes a real two-eyes check and the cooling-off shortens to
"as long as the second person needs".

---

## Reporting bugs

If the bug is non-security:

- Open an issue with reproduction steps.
- Reference the relevant module / ADR if you can.

If the bug is security-significant (auth bypass, key leak, audit
bypass, idempotency bypass, sanctions bypass), see the [security
policy](docs/security.md#reporting-a-vulnerability) — don't open a
public issue.

---

## What this project is NOT going to add

There is a list of explicit non-goals in
[ADR 0001](docs/adr/0001-sync-only-architecture.md) and elsewhere:

- Async / event-loop on the money path.
- Webhooks with delivery guarantees on the service side.
- Per-customer accounting.
- HD wallet derivation.
- Multi-sig schemes.
- Cross-chain support.
- Hosted SaaS.

PRs that add these will be politely declined. If you think one of
them belongs, open an issue and make the case — but read the existing
ADRs first.

---

## Questions

Open an issue, or if you'd rather discuss privately,
`vampir15551 [at] users.noreply.github.com`.
