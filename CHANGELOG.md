# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
loosely and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The CI gate enforces that any code-touching PR adds a line under
`[Unreleased]`. Don't rewrite past sections — only append, then on a
release move `[Unreleased]` → `[X.Y.Z] — YYYY-MM-DD`.

## [Unreleased]

## [1.0.0] — 2026-04-29

This release **merges the previously-separate `skr-crypto` (CLI) and
`skr_crypto-payouts` (service) repositories into a single Python
package**. One install gives you both binaries, versioning and CI
collapse into one pipeline, and the on-disk layout simplifies
significantly. The old `skr_crypto-payouts` repository has been
archived; all subsequent work happens here.

### Breaking changes
- **Single package, two entry points.** `pip install skr-crypto[server]`
  installs both `skr-crypto` (CLI) and `skr-crypto-server` (service).
  `pip install skr-crypto` (no extra) installs the CLI alone — useful
  on operator laptops that only manage a remote install. There is no
  separate service repository to clone.
- **`install` no longer clones.** `skr-crypto install` now writes
  `.env` + creates `data/` only; it does not run `git clone` or build a
  venv. The service code is part of the same package. Flags
  `--git-url`, `--ref`, `--from-path`, `--no-deps`, `--interactive` are
  removed; only `--force` remains.
- **`update` no longer pulls.** `skr-crypto update` now points at the
  latest GitHub Release wheel and emits the `pip install -U …`
  command. The CLI does not auto-upgrade the venv it lives in.
- **`require_installed` accepts `.env` only.** Previously checked for
  `app/` too; that directory no longer exists in installs. Backwards-
  compat: existing 0.x installs that have `app/` still work.
- **systemd unit renamed** `payouts.service` → `skr-crypto.service`.
  The lifecycle commands fall back to the legacy name if a unit
  matching it is found, so old deploys keep working.
- **No PyPI publishing.** Distribution is private — wheels are
  attached to GitHub Releases. `release.yml` builds + signs +
  attaches `SHA256SUMS.txt` alongside the wheel and sdist.

### Added
- **`skr-crypto keygen`** — generate a fresh TRON private key, derive
  the address, optionally write via the selected backend (`--write-to
  env|file|keychain`). Auto-detects from the installed service's
  `KEY_PROVIDER` if no flag given. Refuses to overwrite an existing
  file. Hex value shown once and only once.
- **Process-wide `/send` serialisation** (server). A single
  `threading.Lock` queues concurrent `/send` calls so 5-7 RPC bursts
  per request don't compound into TronGrid 429 cascades on the
  free tier. Tradeoff: lower peak throughput, no rate-limit storms.
- **Inner-try error wrapping in `/send`** (server). Transient TronGrid
  failures during preflight (balance / TRX / destination / estimate)
  now land in audit as `SEND_FAILED rpc_failed` with a clean 5xx
  instead of escaping as ASGI tracebacks.
- **Cloudflare quick-tunnel in `run.sh`**. Local dev gets a temporary
  public `https://*.trycloudflare.com` URL pointing at the local API
  if `cloudflared` is installed. Disable with `NO_TUNNEL=1`.
- **Mermaid diagrams in ARCHITECTURE.md** — components, payout
  sequence (with failure branches), and a private-key data-flow
  diagram with explicit forbidden edges.
- **`docs/adr/`** — four Architecture Decision Records documenting
  sync-only architecture, KeyProvider abstraction, MSK-day
  reconciliation, and audit hard-error policy. Plus an index README.
- **`runbooks/`** — five focused incident playbooks split out of
  OPERATIONS.md: stuck-broadcast, rpc-outage, key-rotation,
  audit-recovery, unresolved-key. Each leads with a Symptom / Cause /
  TL;DR fix triplet for stressed on-call eyes.
- **`audits/`** — committed `bandit` and `pip-audit` reports per
  release, with a README describing the process and the rationale
  for each annotated finding.
- **`GUIDELINES.md`** — terse engineering principles document.
- **`SECURITY.md` rewrite** — formal threat model with assets,
  adversaries, invariants, and explicit non-defences. Supported-
  versions table. GitHub Security Advisory as the preferred channel.
- **`RELEASING.md`** — operational release runbook, replacing the
  shorter `RELEASE.md`.
- **`.github/CODEOWNERS`** — money-path files routed to the owner for
  review on every PR.
- **YAML issue forms** — `bug.yml`, `incident.yml`, `feature.yml`
  with required fields (txid, idempotency_key, severity dropdown,
  doctor-output paste box).
- **CI: `install-test.yml`** — verifies the released wheel installs
  cleanly from the GitHub Release URL on Ubuntu + macOS × Python
  3.11 + 3.12, including `SHA256SUMS.txt` verification.
- **CI: `audit` job in `tests.yml`** — runs `bandit -ll` and
  `pip-audit` on every PR.
- **`SHA256SUMS.txt`** attached to every GitHub Release alongside
  the wheel + sdist.

### Changed
- **Pinned away CVEs.** `python-dotenv >= 1.2.2` (CVE-2026-28684,
  symlink follow on `set_key`; not exploitable for us but bumped for
  hygiene). `starlette >= 0.49.1` (CVE-2025-54121 multipart DoS,
  CVE-2025-62727 Range header DoS).
- **`tarfile.extractall` hardened** (`skr-crypto restore`) — uses
  Python 3.12's `filter='data'` to drop unsafe member types in
  addition to the pre-existing path-traversal check.
- **`status` reports package version** instead of git ref. The
  service code is part of the package now; "git ref of the install"
  is no longer a meaningful thing.
- **README rewritten** — badges, ToC, before/after example, what-ships
  table, two-minute walkthrough, documentation index.
- **OPERATIONS.md slimmed** to ~120 lines as the operations *index*
  pointing at `runbooks/`. The verbose how-to content moved into
  individual runbook files.

### Removed
- `app/` directory and the corresponding repository (`skr_crypto-payouts`).
- `skr_crypto/cli/installer.py` (git clone / venv creation helpers,
  obsolete in the single-package model).
- `tests/test_commands_install.py` (exercised the old `--from-path`
  flow). New install tests live in `test_commands_install.py` of the
  next release if needed.
- `--git-url`, `--ref`, `--from-path`, `--no-deps`, `--interactive`
  flags on `install`.

### Security
- Threat model formalised in `SECURITY.md` with explicit invariants:
  idempotency hard contract, audit immutable on success path, key
  never on disk under env/keychain/1password, key never exits process
  memory, CLI read-only on the money path.
- All commits + tags signed via SSH (ed25519); `SHA256SUMS.txt`
  alongside every release artefact.

- All commits and tags now signed via SSH (operator's existing
  ed25519 key). Add the same key to GitHub as a Signing Key for the
  green "Verified" badge.

### Changed
- **Distribution is private now.** `release.yml` no longer publishes
  to PyPI. On `v*.*.*` tag push it builds the wheel + sdist and
  attaches them to a GitHub Release together with the matching
  CHANGELOG section. Consumers install via
  `pip install <release-wheel-url>`.
- README, RELEASE, install docs, and quick-start examples updated to
  drop PyPI references.

## [0.1.0] — 2026-04-28

### Added
- Initial public CLI: `install`, `update`, `start`, `stop`, `restart`,
  `status`, `logs`, `balance`, `check <txid>`, `audit`, `reconcile`,
  `config show/edit/validate`, `backup`, `restore`, `doctor`,
  `version`, `help`.
- `install` auto-generates a fresh high-entropy `AUTH_TOKEN` and
  writes a chmod-600 `.env` with safe defaults. `--from-path PATH`
  installs from a local working tree (dev mode); `--no-deps` skips
  pip for offline / test scenarios.
- `update` shows the CHANGELOG diff between current ref and target
  before applying, snapshots `data/` first (unless `--no-backup`),
  and restarts via auto-detected regime (systemd / compose / direct).
- `audit` reads the local audit log directly (no service round trip),
  supports filtering by event / recipient / time window, and emits
  one JSON record per stdout line in `--json` mode for `jq` piping.
- `doctor` runs nine environment checks (Python version, git, disk
  space, install presence, `.env` permissions, secret strength, key
  provider readiness, data dir, not-as-root) and exits 1 on any
  FAIL — designed to be greppable in CI.
- `config show` masks AUTH_TOKEN / TRONGRID_API_KEY / PRIVATE_KEY_HEX
  by default; `--unsafe-show-secrets` opts back in.
- Stable exit codes (1..9) per error class, used by all commands.
- HTTP client (`api.py`) with strict timeouts and explicit error
  mapping (401 → AuthError, 5xx → BadResponseError, refused →
  ServiceUnreachableError).
- Lifecycle abstraction (`service.py`) auto-detects systemd / docker
  compose / direct based on filesystem cues, with `--via` override.
- Test suite: 82 tests covering CLI top-level, all command happy +
  sad paths, .env parsing, sensitive-key masking, regime detection.
- CI: GitHub Actions for tests on every push (Python 3.11 + 3.12),
  CHANGELOG-touched gate on PRs (Dependabot exempt), private wheels
  attached to GitHub Releases on `v*.*.*` tags. (No PyPI publishing
  — distribution is private.) MkDocs workflow disabled until docs
  hosting is decided.
- Docs: README, INSTALL, COMMANDS reference, SECURITY model,
  RELEASE checklist.
