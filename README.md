# skr-crypto

> A USDT TRC-20 treasury — synchronous payouts service plus operator CLI in one Python package. Multi-wallet, encrypted at rest, idempotent across crashes, OFAC-screened, audited with fsync.

[**📚 Full documentation**](https://vampir15551.github.io/skr-crypto/) &nbsp;·&nbsp; [Install](#install) &nbsp;·&nbsp; [Quick start](#quick-start) &nbsp;·&nbsp; [Architecture](https://vampir15551.github.io/skr-crypto/architecture/) &nbsp;·&nbsp; [Security](https://vampir15551.github.io/skr-crypto/security/) &nbsp;·&nbsp; [Changelog](CHANGELOG.md)

![version](https://img.shields.io/badge/version-1.4.0-informational)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![signed commits](https://img.shields.io/badge/commits-signed-success)
![status](https://img.shields.io/badge/status-beta-orange)

---

## What this is

A small, opinionated, single-binary Python package that:

- holds N TRON private keys (encrypted at rest, decrypted only in process memory),
- exposes a tiny authenticated HTTP API for sending USDT TRC-20 transfers,
- guarantees every transfer is **idempotent**, **audited**, **risk-checked**, and **sanctions-screened** before it ever hits the chain,
- runs on a laptop, a VPS, or a container with the same code path,
- and refuses to do anything else.

If you want a feature-rich payment platform with dashboards and webhooks, this is not it. If you want a 7,000-line piece of code that you can read in an afternoon and trust to move money — read on.

---

## What ships in `pip install skr-crypto[server]`

```text
pip install 'skr-crypto[server]'
├── skr-crypto         ← operator CLI (always installed)
└── skr-crypto-server  ← FastAPI service (only with [server] extras)
```

Same code, same version, same tests. The `[server]` extra pulls in `fastapi`, `tronpy`, `cryptography`, `prometheus-client`. Without it you get the bare CLI — useful only as a remote client of an already-running service.

| Layer | What it gives you |
|---|---|
| **Multi-wallet pool** | N keys per process. Auto-pick by max USDT balance, or explicit `wallet=NAME` per request. |
| **5 key providers** | `1password` / `env` / `file` / `keychain` / `encrypted_file` (AES-256-GCM, multi-wallet, container-safe). |
| **HTTP API** | FastAPI + `X-API-Key`. `/send`, `/balance`, `/wallets`, `/risk/{addr}`, `/health{,/live}`, `/metrics`. |
| **Audit log** | Append-only JSON-line, fsync per record, **hard-error on disk failure** (returns 500). |
| **Idempotency** | SQLite WAL state machine — survives process crashes; `UNKNOWN` slot = manual reconcile required. |
| **Risk preflight** | 8 checks before broadcast: validity, burn-pattern, activation, contract destination, Tether blacklist, OFAC SDN, TronScan reputation, MistTrack AML. |
| **Reconciliation** | MSK-day startup self-check: yesterday's audit ↔ on-chain receipts. Best-effort. |
| **Metrics** | Prometheus text exposition with stable label cardinality. |
| **CLI** | 18 commands: `install`, `start/stop/restart`, `status`, `balance`, `wallet {list,add,generate,encrypt,...}`, `risk`, `audit`, `reconcile`, `check-tx`, `backup/restore`, `doctor`, ... |

---

## Install

The repo is private; wheels are attached to each [GitHub Release](https://github.com/vampir15551/skr-crypto/releases). The `latest/download/...` URL pattern requires the wheel filename to match the tagged version exactly — easiest is to install **from the tag**:

### From a tagged release (recommended)

```bash
# Latest stable
pip install 'skr-crypto[server] @ git+https://github.com/vampir15551/skr-crypto.git@v1.4.0'
```

You'll need an SSH key or PAT with read access to the repo (it's private). If `git+https` doesn't work (no PAT / public-only token), use SSH:

```bash
pip install 'skr-crypto[server] @ git+ssh://git@github.com/vampir15551/skr-crypto.git@v1.4.0'
```

### From a release wheel

If you've already downloaded the wheel (or are pinning a specific version):

```bash
# Always-latest using gh CLI:
gh release download --repo vampir15551/skr-crypto --pattern '*.whl' --dir /tmp
pip install "/tmp/$(ls -1 /tmp/skr_crypto-*.whl | head -1)[server]"

# Or specific version directly:
pip install \
  'https://github.com/vampir15551/skr-crypto/releases/download/v1.4.0/skr_crypto-1.4.0-py3-none-any.whl#egg=skr-crypto[server]'
```

> **Why your earlier `pip install ...latest/download/skr_crypto-1.0.0-py3-none-any.whl` 404'd:**
> the `latest/download/` redirect points at the *latest tag's* assets, but the **wheel filename itself includes the version**. The wheel under `latest/download/` is `skr_crypto-1.4.0-py3-none-any.whl`, not `1.0.0`. Either install from the tag (above), or always reference the wheel by its current versioned name.

### From source (for development)

```bash
git clone git@github.com:vampir15551/skr-crypto.git && cd skr-crypto
python3.12 -m venv venv && source venv/bin/activate
pip install -e '.[server,dev,docs]'
```

### Docker

```bash
git clone git@github.com:vampir15551/skr-crypto.git && cd skr-crypto
cp .env.example .env  # edit AUTH_TOKEN, KEY_PROVIDER, etc.
docker compose up -d --build
```

Full Docker walkthrough: [Docker deployment](https://vampir15551.github.io/skr-crypto/deployment/docker/).

---

## Quick start

After install:

```bash
# 1. Interactive wizard — writes .env (chmod 600) and data/.
skr-crypto install

# 2. Initialise an encrypted multi-wallet keystore + add a wallet.
skr-crypto wallet encrypt -o data/keystore.json
skr-crypto wallet generate main           # ← prints the address; fund it

# 3. Start the service.
skr-crypto start                          # or `./run.sh` for foreground + cloudflared tunnel

# 4. Verify.
skr-crypto status
skr-crypto balance
skr-crypto wallet list
```

Send USDT (real money path — only via authenticated HTTP):

```bash
AUTH_TOKEN=$(grep '^AUTH_TOKEN=' .env | cut -d= -f2-)
curl -X POST http://127.0.0.1:8000/api/v1/send \
  -H "X-API-Key: $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "to_address": "TRX9SbJzPbXYK1yw6VaFhCJ7ZKAoGwEPwy",
    "amount": "245.50",
    "idempotency_key": "invoice-2026-04-30-#741"
  }'
```

A second `POST` with the same `idempotency_key` returns the **same** txid with `status: "duplicate"` — even after a process restart.

Three-path detail: [Local on Mac](https://vampir15551.github.io/skr-crypto/deployment/local-mac/) · [Docker](https://vampir15551.github.io/skr-crypto/deployment/docker/) · [VPS + systemd](https://vampir15551.github.io/skr-crypto/deployment/systemd-vps/).

---

## Without skr-crypto / With skr-crypto

**Without** — what a payout looks like when you wire tronpy together yourself:

```python
# Hand-rolled TRC-20 payout. Don't ship this.
from tronpy import Tron
from tronpy.keys import PrivateKey
import json, fcntl, time, requests

priv = PrivateKey(bytes.fromhex(open(".env_secret").read().strip()))
client = Tron(network="mainnet")  # no timeout, no retries
contract = client.get_contract("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")

# Cobbled-together idempotency: a flock + flat file
with open("seen.txt", "a+") as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    if request_id in f.read(): raise SystemExit("dup")
    f.write(request_id + "\n")

txn = (contract.functions.transfer(to, amount).with_owner(addr)
       .fee_limit(40_000_000).build().sign(priv))
resp = txn.broadcast()           # what if this raises mid-way?
print(time.time(), resp)         # "audit log" via stdout
requests.post("https://hooks.example/log", json=resp)  # also no timeout
```

**With** — the same payout, with idempotency, audit, key handling, balance checks, OFAC screening, and 7 other risk checks:

```bash
skr-crypto install
skr-crypto wallet encrypt -o data/keystore.json
skr-crypto wallet generate main
skr-crypto start
# operator never touches the money path again — payouts go via HTTP:
# curl -X POST -H "X-API-Key: $KEY" /api/v1/send -d '{...}'
```

The CLI never broadcasts. That's a hard rule — see [Security model](https://vampir15551.github.io/skr-crypto/security/).

---

## Documentation

The deep documentation lives at **[vampir15551.github.io/skr-crypto](https://vampir15551.github.io/skr-crypto/)**. ~6,200 lines across 27 pages, all built from `docs/` via mkdocs-material. Highlights:

| Concept | Reference | Operations |
|---|---|---|
| [Architecture](https://vampir15551.github.io/skr-crypto/architecture/) | [HTTP API](https://vampir15551.github.io/skr-crypto/api-reference/) | [Local on Mac](https://vampir15551.github.io/skr-crypto/deployment/local-mac/) |
| [Multi-wallet pool](https://vampir15551.github.io/skr-crypto/multi-wallet/) | [CLI: `wallet`](https://vampir15551.github.io/skr-crypto/commands/wallet/) | [Docker](https://vampir15551.github.io/skr-crypto/deployment/docker/) |
| [Key providers](https://vampir15551.github.io/skr-crypto/key-providers/) | [Configuration vars](https://vampir15551.github.io/skr-crypto/configuration/) | [VPS + systemd](https://vampir15551.github.io/skr-crypto/deployment/systemd-vps/) |
| [Encrypted keystore format](https://vampir15551.github.io/skr-crypto/keystore-format/) | [Monitoring & metrics](https://vampir15551.github.io/skr-crypto/monitoring/) | [Migrating to 1.4](https://vampir15551.github.io/skr-crypto/migration-1.4/) |
| [Risk preflight](https://vampir15551.github.io/skr-crypto/risk-preflight/) | [ADRs](https://vampir15551.github.io/skr-crypto/adr/) | [Security model](https://vampir15551.github.io/skr-crypto/security/) |
| [Idempotency](https://vampir15551.github.io/skr-crypto/idempotency/) | | |
| [Audit log](https://vampir15551.github.io/skr-crypto/audit-log/) | | |

### Build the docs locally

```bash
make dev          # pip install -e .[dev,docs]
make docs-serve   # mkdocs serve → http://127.0.0.1:8000
```

### Hosting

The docs site is deployed to **GitHub Pages** at `vampir15551.github.io/skr-crypto/`. The workflow at [`.github/workflows/docs.yml`](.github/workflows/docs.yml) builds and publishes on every push to `main` and on every release tag.

If you keep this repo private and don't want to upgrade to GitHub Pro, you have two free alternatives:

- **Cloudflare Pages** — connect the repo via the Cloudflare GitHub App (works with private repos), set build command `mkdocs build --strict` and output dir `site/`. Custom domain is one click.
- **Self-host** — `mkdocs build` produces a static `site/` directory; serve it with Caddy / nginx on the same VPS as the service, e.g. at `docs.your-domain.com`.

Both are documented in `.github/workflows/docs.yml`.

---

## Status & roadmap

**Beta.** Wire formats stabilised at 1.0.0 for the HTTP API, then evolved twice:

- **1.2** — recipient-risk preflight (TronGrid + TronScan).
- **1.3** — OFAC SDN sanctions screening + MistTrack AML; risk verdict in every audit + log line.
- **1.4** — multi-wallet pool with auto-pick by max USDT; encrypted-file keystore (AES-256-GCM); five key providers.

The internal eat-your-own-dogfood deploy has been running on a single Hetzner box without incident across these releases. Full per-version detail in [CHANGELOG.md](CHANGELOG.md).

**Coming next:**

- Hardware-wallet-shaped key provider (Ledger / KeepKey via `tronpy`'s signer abstraction).
- Per-caller API tokens (today: one global `AUTH_TOKEN`).
- A `wallet rotate-passphrase` first-class command (today: manual decrypt → re-init).

**Deliberately not building:**

- HSM cluster integration, multi-sig, HD wallet derivation, cross-chain support.
- Async runtime ([ADR 0001](docs/adr/0001-sync-only-architecture.md)).
- Webhooks ([ADR rejected — adds delivery-guarantee surface](docs/architecture.md#what-this-codebase-deliberately-does-not-do)).
- Per-customer accounting (your back-office's job).

---

## Project layout

```text
skr-crypto/
├── skr_crypto/
│   ├── version.py              # 1.4.0 — single source of truth
│   ├── cli/                    # CLI: install, start, balance, wallet, risk, audit, ...
│   └── server/                 # FastAPI service + everything money-side
├── docs/                       # mkdocs source (deep documentation)
├── tests/                      # ~390 tests, hermetic, no real network
├── .github/workflows/          # test, install-test, release, docs
├── audits/                     # bandit / pip-audit reports per version
├── deploy/                     # systemd unit, Caddyfile examples
├── Dockerfile                  # multi-stage, runs as appuser uid 1000
├── docker-compose.yml          # single-host deploy with bind-mounted volume
├── pyproject.toml              # package + extras + lint config
├── mkdocs.yml                  # docs site config
├── run.sh                      # local dev launcher (cloudflared + 1Password unlock)
└── Makefile                    # common dev targets
```

---

## Contributing

PRs welcome that:

- Fix bugs (start with a failing test).
- Improve docs (`docs/` is the source of truth).
- Add an ADR if you change architecture (`docs/adr/`).
- Pass `make test && make lint && make docs`.

PRs that won't be accepted:

- Add a money-moving CLI command (`send`, `transfer`, etc.). Money moves through the API only.
- Replace the sync mainline with async ([ADR 0001](docs/adr/0001-sync-only-architecture.md)).
- Soften the audit hard-error to a warning ([ADR 0004](docs/adr/0004-audit-hard-error.md)).
- Add a "config reload without restart" feature.

See [`docs/adr/`](docs/adr/) for the full set of architectural decisions.

---

## License

MIT — see [`LICENSE`](LICENSE).
