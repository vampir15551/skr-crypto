<!-- Short description of what changes and why. Link to any issue this closes. -->

## Summary

-

## Risk profile

Tick the boxes that apply. Reviews on red-flagged paths walk the diff
with `SECURITY.md` and `GUIDELINES.md` open.

- [ ] **Money path** — touches `skr_crypto/server/tron_client.py`,
      `routes.py`, `key_providers.py`, `security.py`, `idempotency.py`,
      `audit.py`, or `startup_check.py`
- [ ] **Mainnet config change** — defaults in `skr_crypto/server/config.py`
- [ ] **Release pipeline** — `.github/workflows/release.yml` or `RELEASING.md`
- [ ] **Schema / on-disk format** — `idempotency.db`, `audit.log` shape
- [ ] **Pure docs / lint / test infra** — none of the above

## Checklist

- [ ] Tests added / updated and passing locally (`pytest`)
- [ ] `ruff check skr_crypto tests` clean
- [ ] `CHANGELOG.md` line under `[Unreleased]` describing this change
      (skip only if pure-doc / pure-test PR)
- [ ] `--help` / `skr-crypto help` reflects any new commands or flags
- [ ] No new code path can move money via the CLI — see `SECURITY.md`
- [ ] If money-path: did the diff break any of the invariants in
      `GUIDELINES.md` (sync-only, audit-first, RPC timeouts,
      idempotency contract)?

## Test notes

How you verified the change manually. Include the exact commands and
their expected output. For money-path changes, include a
testnet (`nile` or `shasta`) reproduction.
