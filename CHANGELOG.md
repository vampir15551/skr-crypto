# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
loosely and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The CI gate enforces that any code-touching PR adds a line under
`[Unreleased]`. Don't rewrite past sections — only append, then on a
release move `[Unreleased]` → `[X.Y.Z] — YYYY-MM-DD`.

## [Unreleased]

## [1.3.0] — 2026-04-29

The "real AML" release. Risk preflight gains an OFAC SDN sanctions
check (always-on, zero rate limit, local list) and an opt-in
MistTrack AML score lookup. TronScan goes from opt-in to default-on,
matching the existing Tier 1 TronGrid checks. Every /send now
records the recipient's risk verdict in the audit and success log.

### Added
- **`skr_crypto.server.sanctions`** — OFAC SDN sanctions list
  manager. Downloads the TRX-tagged list from the community-curated
  ``0xB10C/ofac-sanctioned-digital-currency-addresses`` repo at
  process startup, caches in memory + on disk
  (``$SKR_CRYPTO_HOME/data/ofac-sdn-trx.txt``), falls back to the
  cache if the URL is unreachable. Zero rate limit because checks
  are local set-membership.
- **New risk check `sanctions`** (Tier 1, always on) — flags any
  recipient on the OFAC SDN list as HIGH severity. Catches the most
  legally consequential class of address: sending USDT to an
  OFAC-listed entity may violate US sanctions regardless of whether
  the transaction lands on-chain.
- **New risk check `external_misttrack`** (Tier 2, opt-in via
  ``MISTTRACK_API_KEY``) — calls SlowMist's MistTrack
  ``/v1/risk_score`` endpoint for real AML attribution
  (mixers, scam interaction, hacks). Score ≥ 60 or any
  high-severity tag → HIGH. Without an API key the check
  cleanly SKIPs.
- **Risk level in every /send audit + log line.** ``SEND_SUCCESS``
  audit details now include ``risk=<low|medium|high|skipped>``;
  ``[SEND] SUCCESS`` log line surfaces the same. Forensic search
  for "all transfers that risk-flagged MEDIUM" is now one
  ``grep`` away.
- **New env vars** in `.env`:
  - ``SANCTIONS_LIST_URL`` — override source URL for OFAC list
  - ``SANCTIONS_LIST_REFRESH`` — set ``false`` for offline /
    air-gapped deploys (use cached list only)
  - ``MISTTRACK_API_KEY`` — opt in to the MistTrack tier
- **Lifespan now refreshes sanctions list** on every server boot
  (~1-2s, best-effort). Logged as ``[SANCTIONS] loaded N TRX
  addresses from <url>``.

### Changed
- **`RISK_USE_EXTERNAL` defaults to `true`.** New installs and
  existing ones that don't set the variable will now run TronScan
  on every /send (and ``skr-crypto risk``). Adds ~300ms per /send
  but catches community-tagged scams. Set ``RISK_USE_EXTERNAL=false``
  in `.env` to revert to TronGrid-only.
- The risk module now reports **eight** distinct check slots:
  validity, burn_address, activation, smart_contract,
  usdt_blacklist, **sanctions**, balance, external_tronscan,
  **external_misttrack** (last two only when ``external=True``).

### Security
- The OFAC SDN check is the most important addition since the
  Tether blacklist check in 1.2.0. Combined, /send now refuses on
  three real classes of dangerous recipient (Tether-frozen,
  OFAC-sanctioned, smart-contract destinations) before burning a
  single satoshi of fee_limit.
- Audit + scans refreshed for this release; bandit 0 high (gate
  level), pip-audit 0 known vulnerabilities.

## [1.2.0] — 2026-04-29

The "don't burn money on doomed broadcasts" release. Adds
two-tier wallet-risk preflight, a standalone risk endpoint, and
extends the install wizard with numbered prompts and per-provider
follow-ups (the latter previously rolled into the [Unreleased]
section above).

### Added
- **Wallet-risk preflight.** New module `skr_crypto.server.risk`
  runs five local TRON checks against any recipient address:
  validity, known-burn pattern (TRON null + zero-body fallback),
  on-chain activation, smart-contract destination detection, and
  the **Tether USDT contract `isBlackListed`** call. Plus an
  optional Tier-2 TronScan reputation lookup behind
  ``--external`` / ``RISK_USE_EXTERNAL=true``.
- **`GET /api/v1/risk/{address}`** — read-only endpoint that
  returns a JSON report with the verdict (`low`/`medium`/`high`/
  `invalid`), per-check breakdown, and a balance/activity summary.
  Same logic the /send preflight uses.
- **`POST /api/v1/send` preflight blocking.** Before reserving the
  idempotency slot, /send now runs the risk assessment and refuses
  with **HTTP 400 `RISK_TOO_HIGH`** + audit `SEND_REJECTED
  result=risk_too_high` when the level meets `RISK_BLOCK_LEVEL`.
  Catches Tether-blacklisted recipients and contract destinations
  that would silently revert and burn fee_limit on-chain. The full
  report is attached to the error response so the caller sees
  exactly which checks fired.
- **`skr-crypto risk <address>`** CLI command. Calls the endpoint,
  prints verdict + per-check table, exits 0/10/11/12 by level for
  shell-pipeline use.
- **New env vars** in `.env`:
  - `RISK_BLOCK_LEVEL` — `high` (default) / `medium` / `none`
  - `RISK_USE_EXTERNAL` — `false` (default) / `true`
- **Numbered wizard prompts.** `skr-crypto install` interactive
  mode now uses `ask_choice_numbered` and `ask_yes_no` helpers —
  every multi-choice prompt shows ``1) … 2) … 3) …`` with the
  default marked. Operators can type the number, the value name
  (`env`, `mainnet`), or hit Enter for the default.
- **Per-provider follow-up prompts** in install. Picking
  `KEY_PROVIDER=file` immediately asks `PRIVATE_KEY_FILE`;
  `1password` asks vault/item/field; `keychain` asks
  service/account.
- **`--advanced` flag** for `install` exposes prompts for
  `MIN_TRX_RESERVE`, `MAX_ENERGY_BURN_TRX`, `SHUTDOWN_TIMEOUT`,
  `RATE_LIMIT_MAX`, `RATE_LIMIT_WINDOW`. Hidden behind a yes/no
  toggle in interactive mode so first-time installs stay short.
- **New install flags** for non-interactive deploys: `--key-file`,
  `--op-vault`, `--op-item`, `--op-field`, `--keychain-service`,
  `--keychain-account`, `--advanced`.
- **`tx_rejected_total{reason="risk_too_high"}`** Prometheus
  counter — fires every time risk preflight blocks a /send.

### Changed
- `tests/conftest.py` `mock_tron` fixture now sets risk-clean
  defaults so existing /send tests don't have to opt out of the
  new gate. Tests that exercise risk specifically override
  locally.
- `output.ask_choice` (free-text constrained prompt) preserved
  but new prompts should prefer `ask_choice_numbered`.

### Security
- The `usdt_blacklist` check is the most operationally valuable
  addition: it catches a real, reproducible class of "broadcast
  succeeded but on-chain transfer reverted, fee_limit was burned"
  failures that operators have hit IRL. With
  `RISK_BLOCK_LEVEL=high` (default), those addresses are now
  refused before broadcast.
- Audit + scans refreshed for this release; bandit 0 high (gate
  level), pip-audit 0 known vulnerabilities.

## [1.1.0] — 2026-04-29
  now uses ``ask_choice_numbered`` and ``ask_yes_no`` helpers — every
  multi-choice prompt shows ``1) … 2) … 3) …`` with the default
  marked. Operators can type the number, the value name (`env`,
  `mainnet`), or just hit Enter for the default. Out-of-range or
  non-digit input reprompts cleanly.
- **Per-provider follow-up prompts.** After picking a `KEY_PROVIDER`
  the wizard immediately asks for the relevant extras: `file` →
  `PRIVATE_KEY_FILE`; `1password` → `OP_VAULT` + `OP_ITEM` +
  `OP_FIELD`; `keychain` → `KEYCHAIN_SERVICE` + `KEYCHAIN_ACCOUNT`.
  Closes the gap where you'd pick `file` and then have to dig into
  `skr-crypto config edit` to set the path.
- **`--advanced` flag (and prompt).** Opens a tier-2 set of prompts
  for `MIN_TRX_RESERVE`, `MAX_ENERGY_BURN_TRX`, `SHUTDOWN_TIMEOUT`,
  `RATE_LIMIT_MAX`, `RATE_LIMIT_WINDOW`. Hidden behind a yes/no
  toggle in interactive mode so first-time installs stay short.
- **New install flags** for non-interactive deploys:
  `--key-file`, `--op-vault`, `--op-item`, `--op-field`,
  `--keychain-service`, `--keychain-account`, `--advanced`.
- The wizard's "Next steps" output now branches on the chosen
  `KEY_PROVIDER` — different hint per backend, e.g.
  `op signin` reminder for 1password, exact `keygen --write-to file
  --path` line for the file backend.

### Changed
- `output.ask_choice` (free-text constrained prompt) is preserved
  but new prompts should prefer `ask_choice_numbered` for ergonomics.
- Install tests rewritten to feed numbered answers; 18 scenarios
  cover env / file / 1password / keychain wizard paths plus
  invalid-input reprompts and the `--advanced` flow.

## [1.1.0] — 2026-04-29

Operator-experience release: `skr-crypto install` is now a real
wizard with full automation flags, the Cloudflare tunnel is a
first-class setting in `.env` (no more env-var dance), and tab
completion for bash / zsh / fish ships with the package.

### Added
- **`skr-crypto install` wizard.** Interactive by default when STDIN
  is a TTY; falls through to defaults under `--yes` for automation.
  Every prompt has an explicit flag — combine them freely:
  `--key-provider`, `--network`, `--trongrid-api-key`, `--bind-host`,
  `--bind-port`, `--tunnel/--no-tunnel`, `--gen-key`. Force the
  wizard with `--interactive`. Pass `--force` to wipe an existing
  install (with confirmation in interactive mode).
- **`TUNNEL_ENABLED` in `.env`.** First-class setting written by
  `install`. `run.sh` reads it (in addition to the `NO_TUNNEL=1`
  override env var) and starts the Cloudflare quick-tunnel
  accordingly. No more "I forgot to export NO_TUNNEL".
- **`--gen-key` flag on install.** Chains into `skr-crypto keygen`
  immediately after writing `.env`. The interactive wizard also asks
  about it explicitly.
- **`skr-crypto completion bash|zsh|fish`.** Prints a Click-generated
  shell-completion script. `skr-crypto completion zsh >> ~/.zshrc`
  and tab-complete works for every command, flag, and option.
- **`-q, --quiet` root flag.** Suppresses informational and success
  lines on stderr. Errors and warnings still print. Useful for CI /
  automation where you only care about the exit code.
- **`output.ask` / `output.ask_choice`** helpers for prompts that
  honour TTY detection and EOF cleanly.

### Changed
- **`install` no longer takes `--from-path`, `--no-deps`, `--git-url`,
  `--ref`** — these were removed in 1.0.0 but the help text still
  hinted at them in places. Stripped completely.
- The wizard prints provider-specific next-steps after install
  (different hint for `env` vs `file` vs `1password` vs `keychain`).
- `run.sh` now reads `TUNNEL_ENABLED` from `.env` before deciding
  whether to spawn `cloudflared`. Existing `NO_TUNNEL=1` env var is
  still honoured as an emergency override.

### Security
- No new findings. `bandit` 0 high (gate level), `pip-audit` 0 known
  vulnerabilities. Reports under `audits/bandit-2026-04-29.txt` and
  `audits/pip-audit-2026-04-29.json` refreshed for this release.

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
