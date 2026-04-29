# skr-crypto

> A single Python package shipping a CLI and a FastAPI service for USDT TRC-20 treasury operations on TRON. Operator-only. Sync-only. Audited.

[Install](#install) &nbsp;·&nbsp; [Architecture](ARCHITECTURE.md) &nbsp;·&nbsp; [Operations](OPERATIONS.md) &nbsp;·&nbsp; [Security](SECURITY.md)

---

![version](https://img.shields.io/badge/version-1.0.0-informational)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![signed commits](https://img.shields.io/badge/commits-signed-success)
![status](https://img.shields.io/badge/status-beta-orange)

[Latest release](https://github.com/vampir15551/skr-crypto/releases/latest) — wheels are attached there. The repo is private; CI/coverage badges intentionally omitted because shields.io can't see private workflow status.

---

## TL;DR

- One package, two entry points: a CLI (`skr-crypto`) for operators and a FastAPI service (`skr-crypto-server`) that signs and broadcasts USDT TRC-20 payouts via tronpy + TronGrid.
- Sync-only by mandate. No `asyncio` on the money path. Determinism over throughput.
- Audit-first: every state transition that affects on-chain reality writes a fsync'd JSON-line audit row before the response is returned.

```bash
pip install https://github.com/vampir15551/skr-crypto/releases/latest/download/skr_crypto-1.0.0-py3-none-any.whl
skr-crypto keygen --write-to env
skr-crypto start
skr-crypto balance
```

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

**With** — the same payout, with idempotency, audit, key handling, and balance checks:

```bash
skr-crypto install
skr-crypto start
# operator never touches the money path again — payouts go via HTTP:
# curl -X POST -H "X-API-Key: $KEY" /api/v1/send -d '{...}'
```

The CLI never broadcasts. That's a [hard rule](GUIDELINES.md#no-money-path-in-the-cli).

---

## What ships

| Layer | What it gives you | Where it lives |
|---|---|---|
| **CLI** | 16 commands (install, update, start/stop/restart, status, logs, balance, check, audit, reconcile, config, backup, restore, doctor, keygen, version). Stable exit codes. Wheels signed via release workflow. | `skr_crypto/cli/` |
| **HTTP API** | FastAPI app, `X-API-Key` header auth (constant-time compare), `/api/v1/send`, `/api/v1/balance`, `/api/v1/health/{live,ready}`. OpenAPI/`/docs` are disabled in production by default. | `skr_crypto/server/` |
| **Audit** | Append-only JSON-line log. `fsync` per record. Hard error on write failure — request returns 500. | `skr_crypto/server/audit/` |
| **Idempotency** | SQLite in WAL mode. Crash-safe state machine (`PENDING → COMMITTED / RELEASED / UNRESOLVED`). | `skr_crypto/server/idempotency/` |
| **Reconciliation** | Startup self-check on every boot. Reports `UNRESOLVED` slots refusing to auto-retry. | `skr_crypto/server/reconcile/` |
| **KeyProvider** | Pluggable secret source: `1password://` URI, `env://VAR`, `file://path`, macOS `keychain://`. Key never written to audit, never returned over HTTP, never persisted except via the explicit `file` provider. | `skr_crypto/server/keys/` |
| **Metrics** | Prometheus `/metrics` endpoint. Counters and histograms for payout outcomes, RPC latency, audit writes. | `skr_crypto/server/metrics/` |
| **Diagnostics** | `skr-crypto doctor` walks the environment (Python version, venv, `.env` perms, RPC reachability, audit dir writable, idempotency.db open). Brew-doctor style. | `skr_crypto/cli/doctor.py` |

---

## Install

Three options. Pick one.

**1. Wheel from the latest release** (recommended for hosts):

```bash
pip install https://github.com/vampir15551/skr-crypto/releases/latest/download/skr_crypto-1.0.0-py3-none-any.whl
```

**2. Editable install from source** (for development):

```bash
git clone git@github.com:vampir15551/skr-crypto.git && cd skr-crypto
pip install -e ".[dev]"
```

**3. Docker compose** (one-shot host bring-up):

```bash
docker compose -f docker-compose.yml up -d
```

The compose file lives at `docker-compose.yml`; it expects `.env` next to it. Copy `.env.example` first.

---

## Two-minute walkthrough

```bash
# 1. Lay down the install dir, generate AUTH_TOKEN, create venv.
skr-crypto install

# 2. Mint a TRON keypair (or BYO via `--from-hex`). Persist to .env, chmod 600.
skr-crypto keygen --write-to env

# 3. Start the FastAPI service. Reconciliation runs before listening.
skr-crypto start

# 4. Confirm it's up.
skr-crypto status

# 5. Smoke the liveness probe.
curl -s http://127.0.0.1:8080/api/v1/health/live

# 6. Audit anything that's happened today.
skr-crypto audit --today

# 7. Walk the environment checks.
skr-crypto doctor
```

If `doctor` is green, the service is ready to accept signed payout requests. If it's not, fix what it tells you.

---

## Documentation index

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — system design, sequence diagrams, state stores, failure modes.
- [`OPERATIONS.md`](OPERATIONS.md) — day-2 operations: deploy, monitor, rotate keys, restore from backup.
- `runbooks/` — incident playbooks (audit write failure, RPC outage, UNRESOLVED slot recovery). *To be added.*
- [`SECURITY.md`](SECURITY.md) — threat model, key-handling rules, disclosure policy.
- [`GUIDELINES.md`](GUIDELINES.md) — engineering principles. The bar for accepting a PR.
- [`RELEASE.md`](RELEASE.md) — cut-a-release checklist (tag → workflow → wheel → release notes).
- [`CHANGELOG.md`](CHANGELOG.md) — what changed in each version.
- `docs/adr/` — architecture decision records. *To be added.*
- `docs/` — mkdocs site source. Live at <https://vampir15551.github.io/skr-crypto/> (built from `mkdocs.yml`).

---

## Status & roadmap

**Beta.** The CLI surface has been stable since `0.3.0` and the server's money path was the focus of `1.0.0`. Solid: durable audit, idempotency state machine, deterministic keygen, KeyProvider abstraction, startup reconciliation. The internal eat-your-own-dogfood deploy has been running without incident on a single Hetzner box.

Coming next:
- Receipt polling that runs *postmortem only* — never as a retry trigger. The point is closing the audit loop, not auto-recovering UNKNOWN slots.
- A vetted Hetzner deploy guide as a runbook (systemd unit, backup cron, log rotation, firewall).
- Multi-host: a second worker reading the same `data/` is explicitly out of scope. If we ever need horizontal scale we'll do it with a queue, not shared state.

Things we are deliberately *not* building: HSM integration, multi-sig, HD wallet derivation, cross-chain support, async runtime. See [GUIDELINES.md](GUIDELINES.md) and the non-goals list in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## License

MIT — see [`LICENSE`](LICENSE).
