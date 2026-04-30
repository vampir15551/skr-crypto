<!-- Short description of what changes and why. Link to any issue this closes. -->

## Summary

-

## Risk profile

Tick the boxes that apply. Reviews on red-flagged paths walk the diff
with `docs/security.md`, the relevant ADR, and any invariant tests open.

- [ ] **Money path** — touches a file listed in `.github/CODEOWNERS`
      under "Money path" (`wallet.py`, `wallet_pool.py`, `tron_client.py`,
      `key_providers.py`, `security.py`, `tokens.py`, `idempotency.py`,
      `audit.py`, `risk.py`, `sanctions.py`, `routes.py`, etc.)
- [ ] **Auth / authn surface** — adds, changes, or removes auth checks
      or scope requirements
- [ ] **HTTP wire format** — adds/removes/renames a field on any
      `/api/v1/*` response or request
- [ ] **Mainnet config defaults** — changes a default in `server/config.py`
- [ ] **Release pipeline** — `.github/workflows/release.yml` or `RELEASING.md`
- [ ] **Schema / on-disk format** — `idempotency.db`, `audit.log`,
      keystore JSON
- [ ] **Pure docs / lint / test infra** — none of the above

## Invariant impact

Run `pytest -m invariant --collect-only -q` and confirm:

- [ ] No `@pytest.mark.invariant` test was modified.
- [ ] **OR** an ADR superseding the relevant prior ADR has been added
      and is linked here: <!-- link to docs/adr/00XX-...md -->

The invariant-gate workflow refuses to merge without the ADR. See
`docs/adr/0007-engineering-safety-practices.md` for the rationale.

## Wire-format impact

If any `/api/v1/*` response shape changed:

- [ ] Updated golden file in `tests/fixtures/api_v1_golden/`
- [ ] `pytest tests/test_wire_format_compat.py` still passes
- [ ] `docs/api-reference.md` updated
- [ ] If a field was removed/renamed → marked **BREAKING** in CHANGELOG
      and major version bumped

## Threat-model impact

If this change adds, removes, or modifies a security boundary:

- [ ] `tests/test_threat_model.py` updated with the new / changed
      adversary class
- [ ] `docs/security.md` updated

## Checklist

- [ ] `make test` passes (`pytest`, all 388+)
- [ ] `make lint` clean (`ruff check skr_crypto tests`)
- [ ] `make docs` clean (`mkdocs build --strict`, no warnings)
- [ ] `pip-audit` reports no known vulnerabilities (or pin update is
      explained below)
- [ ] `bandit -lll -r skr_crypto` reports no high-severity findings
- [ ] `CHANGELOG.md` entry added under `[Unreleased]`
      (or PR carries `skip-changelog` label)
- [ ] `--help` / `skr-crypto help` reflects any new commands or flags
- [ ] No new code path can move money via the CLI — see `docs/security.md`

## Cooling-off (single-committer mode)

While the project has one committer (per `CONTRIBUTING.md`):

- [ ] PR has been open for at least 24 hours since the last code change
- [ ] Re-read the diff after the cooling-off period

## Test notes

How you verified the change manually. Include the exact commands and
their expected output. For money-path changes, include a testnet
(`nile` or `shasta`) reproduction.
