# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
loosely and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The CI gate enforces that any code-touching PR adds a line under
`[Unreleased]`. Don't rewrite past sections — only append, then on a
release move `[Unreleased]` → `[X.Y.Z] — YYYY-MM-DD`.

## [Unreleased]

### Added
- `skr-crypto keygen` — generate a fresh TRON private key, derive the
  address, optionally write to the selected backend (`--write-to
  env|file|keychain`). Auto-detects from the installed service's
  `KEY_PROVIDER` if no flag given. Refuses to overwrite an existing
  file. Hex value shown once and only once. Tronpy is lazy-imported
  so the CLI itself doesn't carry the dep weight.
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
