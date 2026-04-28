# SKR Crypto

Operator CLI for the SKR Crypto microservice fleet. Installs, updates,
and observes the underlying payout services. **Money-moving operations
are deliberately not exposed here** — they go through the service's
authenticated HTTP API. See [SECURITY.md](SECURITY.md).

## Quick start

Distribution is private — install from a GitHub Release wheel:

```bash
pip install https://github.com/vampir15551/skr-crypto/releases/latest/download/skr_crypto-0.1.0-py3-none-any.whl
skr-crypto install                  # clones the service, generates AUTH_TOKEN
skr-crypto config edit              # set TRONGRID_API_KEY etc.
skr-crypto keygen --write-to env    # OR: bring your own PRIVATE_KEY_HEX
skr-crypto start
skr-crypto status                   # ✓ running, healthy, version printed
```

Or for development from source: `git clone … && pip install -e .`

## What the CLI does

```text
Lifecycle
  install            Clone the service from git into the install dir
  update             Pull latest tagged release and restart
  start / stop       /restart  Lifecycle (systemd / docker / direct)

Inspect
  status             Running? Healthy? Which version?
  logs               Tail recent logs (-f to follow)
  balance            TRX/USDT + on-chain resources
  check <txid>       Read-only on-chain status look-up
  audit              Browse the durable audit log
  reconcile          Re-run the startup-check on demand
  version            CLI + service versions

Configuration
  config show        With secrets masked
  config edit        Open .env in $EDITOR
  config validate    Run the service's own validate_config()

Backups
  backup             Snapshot data/ → tar.gz with rotation
  restore <archive>  Restore data/ from a backup

Diagnostics
  doctor             Walk environment checks (brew-doctor style)
```

`skr-crypto help` prints this list grouped and colour-coded.
`skr-crypto help <command>` dispatches to the command's full `--help`.

## Install / update flow

```bash
# First time on a host
pip install https://github.com/vampir15551/skr-crypto/releases/latest/download/skr_crypto-0.1.0-py3-none-any.whl
skr-crypto install --git-url https://github.com/vampir15551/skr_crypto-payouts.git

# Roll forward to the latest tagged release
skr-crypto update            # shows CHANGELOG diff, asks to confirm,
                             # snapshots data/, pulls, reinstalls, restarts

# Pin to a specific version
skr-crypto update --version v0.3.0
```

`update` always snapshots `data/` first (unless `--no-backup`). If a
deploy goes wrong, `skr-crypto restore <archive>` rolls the state back
in one command.

## Exit codes

Every error maps to a stable exit code so wrapper scripts can branch:

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Generic error (last-resort) |
| 2 | Not installed |
| 3 | Already installed (use `--force`) |
| 4 | Service unreachable |
| 5 | Auth rejected |
| 6 | Bad/malformed API response |
| 7 | git operation failed |
| 8 | venv / pip install failed |
| 9 | Config error (bad `.env`) |

## Layout on disk

`skr-crypto install` creates this layout in `~/.skr-crypto`
(or `--dir`, or `$SKR_CRYPTO_HOME`):

```text
~/.skr-crypto/
├── .env             # generated; chmod 600
├── app/             # service code (cloned from git)
├── venv/            # python venv
├── data/
│   ├── audit.log
│   └── idempotency.db
├── backups/         # taken by `skr-crypto backup` and `update`
└── ...              # rest of the service repo
```

## Documentation

Full docs at <https://vampir15551.github.io/skr-crypto/> (auto-built
on push to main). The same content is in `docs/` if you want to
read it offline.

## Versioning

SemVer. Pre-1.0 we reserve the right to break things in a minor with
a clear note in `CHANGELOG.md`. See `RELEASE.md` for the cut-a-release
checklist.

## License

MIT — see `LICENSE`.
