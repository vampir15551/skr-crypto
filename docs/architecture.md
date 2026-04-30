# Architecture

This document explains what every module in `skr_crypto/` does, how they fit together, and **why** each non-obvious shape was chosen. It's the document you should read before changing anything.

## Top-level layout

```text
skr_crypto/
├── version.py              # Single source of truth for the package version
├── cli/                    # Operator CLI (Click) — read-only on the money path
│   ├── main.py             # Click root group + lazy command registration
│   ├── api.py              # APIClient — HTTP wrapper around /api/v1
│   ├── commands/           # One module per command, lazy-imported
│   │   ├── install.py      # Interactive wizard
│   │   ├── wallet.py       # `wallet list/add/generate/remove/encrypt/...`
│   │   ├── balance.py      # `balance --wallet`
│   │   ├── risk.py         # `risk <addr>`
│   │   ├── audit.py        # `audit | tail`
│   │   ├── reconcile.py    # MSK-day reconciliation
│   │   └── ...             # 14 more
│   ├── config.py           # .env parsing + install-dir resolution
│   ├── exceptions.py       # ClickException subclasses with stable exit codes
│   └── output.py           # Rich-based renderers (tables, colours)
└── server/                 # FastAPI service — the only code path that signs
    ├── __main__.py         # uvicorn entry point
    ├── server.py           # App factory + lifespan + middleware + handlers
    ├── routes.py           # /api/v1/* — every endpoint
    ├── models.py           # Pydantic schemas
    ├── exceptions.py       # PayoutError tree
    ├── tron_client.py      # Keyless TRON RPC layer
    ├── wallet.py           # Wallet (name + address + PrivateKey + send_usdt)
    ├── wallet_pool.py      # WalletPool with auto-pick by max USDT
    ├── key_providers.py    # 5 backends → list[LoadedWallet]
    ├── encrypted_keystore.py   # AES-256-GCM JSON keystore
    ├── security.py         # Bridges providers → WalletPool, X-API-Key auth
    ├── risk.py             # 8-check recipient risk preflight
    ├── sanctions.py        # OFAC SDN list manager (local, zero-rate-limit)
    ├── idempotency.py      # State machine + SQLite WAL store
    ├── audit.py            # Append-only JSON audit with fsync
    ├── metrics.py          # Prometheus counters / histograms / gauges
    ├── startup_check.py    # MSK-day audit ↔ on-chain reconciliation
    ├── shutdown.py         # Inactivity-driven auto-shutdown
    ├── net.py              # X-Forwarded-For-aware client_address
    ├── logging_config.py   # JSON or human-text logging selector
    └── config.py           # Env-driven configuration + validation
```

7,000-ish lines. ~3,500 tests. The line count is on purpose.

---

## Request lifecycle: `POST /api/v1/send`

```mermaid
sequenceDiagram
    autonumber
    participant C as Caller
    participant MW as Middleware<br/>(rate limit, X-Request-ID)
    participant H as send handler<br/>(routes._send_impl)
    participant W as WalletPool
    participant R as Risk module
    participant I as Idempotency
    participant T as TronClient
    participant Wal as Wallet.send_usdt
    participant A as Audit (fsync)
    participant Tg as TronGrid

    C->>MW: POST /send (X-API-Key, body)
    MW->>MW: 429 if over budget
    MW->>H: forward
    H->>W: resolve(req.wallet)<br/>(named OR auto-pick by USDT)
    W-->>H: Wallet
    H->>H: validate base58check addr
    H->>R: assess_risk(to_addr, external=true)
    R->>Tg: account / contract / balance / blacklist
    R->>R: OFAC SDN local lookup
    R->>Tg: TronScan reputation
    R-->>H: RiskReport(level=low|medium|high|invalid)
    alt level >= block_level
        H->>A: SEND_REJECTED (risk_too_high)
        H-->>C: 400 RISK_TOO_HIGH + report
    end
    H->>I: reserve(idempotency_key)
    alt slot already committed
        H->>A: SEND_DUPLICATE
        H-->>C: 200 status=duplicate, txid=<existing>
    end
    H->>T: get_usdt_balance_for(wallet.address)
    H->>T: get_trx_balance_for(wallet.address)
    H->>T: estimate_transfer_energy(...)
    alt estimate * price > MAX_ENERGY_BURN_TRX
        H->>A: SEND_REJECTED (energy_too_expensive)
        H-->>C: 400 ENERGY_TOO_EXPENSIVE
    end
    H->>Wal: send_usdt(client, contract, to, amount, fee_limit_sun)
    Wal->>Tg: build → sign → broadcast
    Tg-->>Wal: {result:true, txid:"abc..."}
    Wal-->>H: txid
    H->>I: commit(idempotency_key, txid)
    H->>A: SEND_SUCCESS (fsync)
    H-->>C: 200 {txid, wallet, from_address, ...}
```

The `_send_lock` (process-wide `threading.Lock`) is acquired at step 4 and released after step 21. Concurrent /send requests queue. This is intentional — see [ADR 0001](adr/0001-sync-only-architecture.md).

---

## The keyless TronClient + Wallet split

Until 1.3.x, `tron` was a singleton holding the HTTP client **and** the private key. Multi-wallet broke that — there are now N keys per process, but only one HTTP connection pool. So 1.4.0 split the model:

```mermaid
classDiagram
    class TronClient {
        +client: Tron
        -_usdt_contract
        -_energy_price_sun
        +init()
        +get_trx_balance_for(addr) Decimal
        +get_usdt_balance_for(addr) Decimal
        +get_resource_summary_for(addr) dict
        +estimate_transfer_energy(from, to, amount) int|None
        +energy_price_sun() int
        +compute_fee_limit_sun(estimate) int
        +get_destination_info(addr) dict
        +get_usdt_contract()
        +check_connection() bool
    }
    class Wallet {
        +name: str
        +address: str
        +priv_key: PrivateKey
        +send_usdt(client, contract, to, amount, fee_limit_sun) str
        +destroy()
    }
    class WalletPool {
        -_wallets: dict[str, Wallet]
        +init(wallets)
        +resolve(name, *, tron_client) Wallet
        +names() list[str]
        +all() list[Wallet]
        +count() int
        +destroy()
    }
    WalletPool "1" o-- "N" Wallet : holds
    Wallet ..> TronClient : uses for build/broadcast
```

**Invariants:**

- `TronClient` never sees a `PrivateKey`. Anything that signs is in `Wallet.send_usdt`.
- `WalletPool` is the only place that maps a `name` → `Wallet`.
- Endpoints **never** read `tron.address` or hold a wallet reference between requests.

This makes the blast radius of any signing-related change one file: `wallet.py`.

---

## Configuration is env-driven, validated at boot

```mermaid
flowchart TD
    DotEnv[.env file] -->|python-dotenv| Env[os.environ]
    Env --> Cfg[server/config.py]
    Cfg -->|module-level constants| Modules[Every server module]
    Cfg -->|validate_config| Boot[__main__.py]
    Boot -->|exit 1 on bad config| K[Process killed]
    Boot -->|OK| Lifespan[FastAPI lifespan]
```

`config.py` reads everything at import time and exposes module-level constants. `validate_config()` runs in `__main__` before uvicorn starts and `sys.exit(1)`s on invalid combinations (e.g. `KEY_PROVIDER=encrypted_file` without `KEYSTORE_FILE`).

Tests reload `config.py` after monkeypatching env vars — see `tests/conftest.py`'s pre-import dance for why dotenv has to be neutralised.

---

## Lifespan: what runs at boot, in order

```mermaid
flowchart TD
    Start([uvicorn starts]) --> ValidateConfig[validate_config<br/>exit 1 on errors]
    ValidateConfig --> InitTron[tron.init<br/>HTTP client + endpoint]
    InitTron --> LoadWallets[security.load_wallets<br/>via configured KeyProvider]
    LoadWallets --> InitPool[wallets.init<br/>populate WalletPool]
    InitPool --> Sanctions[sanctions.load<br/>OFAC SDN refresh]
    Sanctions --> Reconcile[startup_check<br/>MSK-day audit ↔ on-chain]
    Reconcile --> Ready[Service ready<br/>200 OK on /health/live]
    Ready --> Stop([SIGTERM])
    Stop --> Destroy[wallets.destroy<br/>tron.destroy<br/>idempotency.close<br/>audit.close<br/>provider.lock]
```

**Why the order matters:**

- Wallet pool must be populated before any /send arrives. The pool is checked by every wallet-resolving endpoint.
- Sanctions list refresh is **best-effort**. A network failure logs a warning and continues — the on-disk cache is the fallback (see [ADR 0002](adr/0002-pluggable-key-providers.md) for the same pattern with key providers).
- The MSK reconciliation is **best-effort** too — see [ADR 0003](adr/0003-msk-day-reconciliation.md). It compares yesterday's `SEND_SUCCESS` audit records against on-chain receipts.
- Shutdown is reversed: wallets first (so we stop being able to sign), then connections, then storage, then the secret store.

---

## Three storage layers, three durability stories

| Storage | Used for | Durability | Recovery |
|---|---|---|---|
| **Audit log** (`audit.log`) | Compliance trail of every SEND_* event | Append-only, fsync per record, hard-error on write fail (5xx) | Re-run `startup_check`; manual reconciliation with TronScan |
| **Idempotency DB** (`idempotency.db`) | Replay protection across restarts | SQLite WAL, fsync on commit | Orphaned PENDING rows promoted to UNKNOWN at boot → caller sees `IDEMPOTENCY_UNRESOLVED` 409 |
| **Sanctions cache** (`ofac-sdn-trx.txt`) | OFAC list when offline | Plain file overwritten on each successful refresh | None needed — list is reloaded on next boot |

The audit log and idempotency DB are the only files you must back up. Lose them and you lose the ability to detect double-broadcasts.

---

## Risk preflight, in detail

The risk module runs eight checks in order, returning the worst severity:

```mermaid
flowchart LR
    Addr[recipient address] --> V[1. validity]
    V --> B[2. burn pattern]
    B --> Act[3. activation]
    Act --> SC[4. smart contract]
    SC --> BL[5. USDT blacklist]
    BL --> S[6. OFAC sanctions]
    S --> TS[7. TronScan reputation]
    TS --> MT[8. MistTrack AML]
    MT --> Verdict{Worst severity}
    Verdict -->|invalid| Block1[/send → 400 INVALID_ADDRESS/]
    Verdict -->|high| Block2[/send → 400 RISK_TOO_HIGH/]
    Verdict -->|medium / low| Pass[/send proceeds, level audited/]
```

Checks 1–6 are **always on** and have no rate limit. Checks 7–8 are external HTTP calls, gated by `RISK_USE_EXTERNAL` (default true) and `MISTTRACK_API_KEY` respectively.

A single failed check doesn't always block — only `FAIL` at or above `RISK_BLOCK_LEVEL` does. The full machinery: [Risk preflight](risk-preflight.md).

---

## Idempotency state machine

```mermaid
stateDiagram-v2
    [*] --> PENDING: reserve(key)
    PENDING --> COMMITTED: commit(key, txid)
    PENDING --> released: release(key)<br/>(after preflight failure)
    released --> [*]
    COMMITTED --> COMMITTED: subsequent reserve(key)<br/>returns existing txid
    PENDING --> UNKNOWN: process crash + restart
    UNKNOWN --> [*]: caller gets 409<br/>IDEMPOTENCY_UNRESOLVED
```

A key in `UNKNOWN` is **dangerous**: we don't know if its broadcast happened. The service refuses retries — the operator must reconcile manually (TronScan + audit log) before sending again with the same key. Full details: [Idempotency](idempotency.md).

---

## Operator CLI vs the service

The CLI is a **read-only client of the API** plus an **install-time keystore manager**. There is no `skr-crypto send` command. There never will be. Money moves through the authenticated HTTP API only — see [Security model](security.md) for why.

Two commands write to disk:

- `skr-crypto install` — wizard, writes `.env` (chmod 600) and `data/`.
- `skr-crypto wallet ...` — for `KEY_PROVIDER=encrypted_file`, writes to the keystore JSON. For other providers, prints the env-var / file layout to apply manually.

Every other command either reads the local `.env`, hits `/api/v1/...`, or shells out to systemd / docker.

---

## Tested invariants

The test suite (~390 tests) enforces the invariants that make this thing trustworthy:

- **No HTTP escapes the test process.** Tests that forget to mock fail loud (connection refused). `tests/conftest.py` neutralises dotenv before any server import so a stray prod `.env` can't leak in.
- **Audit fsync failure is a 5xx.** `test_audit.py::test_audit_write_failure_returns_500`.
- **Empty/missing txid never gets cached as success.** `test_tron_client.py::TestSendUsdtBroadcastResult::test_empty_txid_raises`.
- **Idempotency survives a crash.** `test_idempotency.py::test_orphaned_pending_promoted_to_unknown`.
- **OFAC blocks /send.** `test_routes_risk.py::test_sanctioned_address_blocked`.
- **Multi-wallet auto-pick uses max USDT.** `test_wallet_pool.py::test_auto_pick_returns_max_usdt`.

If you change something and one of these flips, the change is wrong — not the test.

---

## What this codebase deliberately does NOT do

- **No async on the money path.** [ADR 0001](adr/0001-sync-only-architecture.md).
- **No webhooks.** Callers poll `check-tx` or watch their own ledger. Webhooks add a delivery-guarantee surface we don't want.
- **No per-customer accounting.** The service moves money; your back-office tracks who's owed what.
- **No second TRC-20 token by default.** `USDT_CONTRACT` is configurable, but only one token per process.
- **No multi-currency.** TRX-based USDT only.
- **No queue / batch endpoint.** Each /send is one transfer. Batching would obscure the audit trail and complicate idempotency.
- **No "soft" audit failure.** [ADR 0004](adr/0004-audit-hard-error.md).

These are not features waiting to be added. They are explicit non-goals. Every one of them came up at least once and was rejected with a paragraph of reasoning that lives somewhere in this repo.
