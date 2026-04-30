# Running locally on a Mac

This is the "I want to try the service end-to-end without committing to anything" path. ~10 minutes from clone to first successful `/send` against testnet.

For Docker on Mac, see [Docker](docker.md). For a real production deploy, see [VPS + systemd](systemd-vps.md).

---

## Prerequisites

```bash
# Python 3.12 (3.11 also works)
brew install python@3.12

# Optional but recommended
brew install --cask 1password 1password-cli       # for KEY_PROVIDER=1password
brew install cloudflared                          # for the run.sh public tunnel
```

---

## Install + first wallet

```bash
git clone https://github.com/vampir15551/skr-crypto.git
cd skr-crypto

python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[server]"           # editable install — pick up code changes without reinstall

# Interactive wizard — writes .env (chmod 600) and data/
skr-crypto install
```

The wizard asks:

1. **Key provider** — pick `5` for `encrypted_file` if you want the multi-wallet encrypted-keystore path.
2. **Keystore path** — accept the default `data/keystore.json`.
3. **TRON network** — `nile` for testnet (free TRX from faucet), `mainnet` for production.
4. **TronGrid API key** — paste yours, or leave blank for the rate-limited free tier.
5. **Bind host / port** — defaults are fine.
6. **Cloudflare tunnel** — `n` unless you want a public URL via `cloudflared`.

After the wizard, initialise the keystore and add a wallet:

```bash
skr-crypto wallet encrypt -o data/keystore.json
# Enters a passphrase twice.
# Save it somewhere safe — losing it means losing every key in the file.

skr-crypto wallet generate main
# Generates a fresh TRON private key, encrypts it under the passphrase, prints the address.
# That address is what you fund.
```

If you're on Nile testnet, fund the address from the [Nile faucet](https://nileex.io/join/getJoinPage). On mainnet, send TRX + USDT to it.

---

## Start the service

Two options:

### Option A: `run.sh` (recommended for dev)

```bash
./run.sh
```

What it does:

- Unlocks 1Password if `KEY_PROVIDER=1password` and `op` is on PATH (Touch ID prompt).
- Starts a Cloudflare quick tunnel if `cloudflared` is installed → prints a public `https://*.trycloudflare.com` URL.
- Starts `skr-crypto-server` in foreground.

**Disable the tunnel** for this run only: `NO_TUNNEL=1 ./run.sh`. Permanently: set `TUNNEL_ENABLED=0` in `.env`.

**Provide the keystore passphrase:**

The service prompts for the passphrase via `getpass` if `KEY_PASSPHRASE` and `KEY_PASSPHRASE_FILE` are unset and stdin is a TTY. So you'll see:

```
Keystore passphrase:
```

Type it. The service starts.

For non-interactive starts (e.g. via a launchd agent), put the passphrase in a chmod-600 file and set `KEY_PASSPHRASE_FILE=/path/to/file` in `.env`.

### Option B: `skr-crypto start` (uses systemd / launchd if available)

```bash
skr-crypto start                # daemonize
skr-crypto status               # check it's healthy
skr-crypto logs --tail 50       # tail logs
skr-crypto stop                 # stop
```

This path uses your platform's service manager. On macOS without launchd integration it falls back to a backgrounded `nohup`. Mostly a convenience wrapper.

---

## Verify

```bash
# Liveness — no auth needed
curl http://127.0.0.1:8000/api/v1/health/live
# {"status":"ok","uptime_seconds":42}

# Authenticated health
AUTH_TOKEN=$(grep '^AUTH_TOKEN=' .env | cut -d= -f2-)
curl -H "X-API-Key: $AUTH_TOKEN" http://127.0.0.1:8000/api/v1/health
# {"status":"ok","network":"nile","wallet_count":1,"wallet_names":["main"],...}

# Balance
curl -H "X-API-Key: $AUTH_TOKEN" http://127.0.0.1:8000/api/v1/balance
# {"wallet":"main","address":"T...","trx":"100","usdt":"500",...}

# Or via CLI:
skr-crypto health
skr-crypto balance
skr-crypto wallet list
```

---

## First `/send`

A real test transfer — you'll need:

- **Source wallet** funded with USDT and at least 50 TRX (`MIN_TRX_RESERVE`).
- **Recipient address** — e.g. another wallet you control. On Nile, generate one with another `skr-crypto wallet generate test-recipient`.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/send \
  -H "X-API-Key: $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "to_address": "TYourTestRecipientxxxxxxxxxxxxxxxxxx",
    "amount": "1.0",
    "idempotency_key": "first-test-send"
  }'
```

Expected response:

```json
{
  "txid": "abcdef...",
  "from_address": "TMainxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "wallet": "main",
  "to_address": "TYourTestRecipientxxxxxxxxxxxxxxxxxx",
  "amount": "1.0",
  "idempotency_key": "first-test-send",
  "status": "broadcast"
}
```

Verify on-chain:

```bash
skr-crypto check-tx abcdef...
# {"txid":"abc...","status":"SUCCESS","confirmations":12,...}
```

Run the same `curl` command a second time — you'll get the **same** `txid` with `"status": "duplicate"`. That's idempotency working.

---

## Adding a second wallet

```bash
skr-crypto wallet generate cold
# Address: TColdxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# Restart the service for the new wallet to become signable: skr-crypto restart
```

After restart:

```bash
skr-crypto wallet list
# ┏━━━━━━━━━━━━━━━━━━━ Wallets (auto-pick: main) ━━━━━━━━━━━━━━━━━━━┓
# ┃ Wallet  Address        USDT     TRX    Energy                  ┃
# ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
# │ cold    TColdxxxx...   0        0      0                       │
# │ main    TMainxxxx...   500      100    6500                    │
# └────────────────────────────────────────────────────────────────┘
```

`/send` without a `wallet` field auto-picks `main` (more USDT). Pass `"wallet": "cold"` to force a specific source.

---

## Stopping cleanly

If you're using `run.sh`, just `Ctrl-C`. The service:

1. Stops accepting requests.
2. Drains in-flight `/send` (the global lock).
3. Closes the SQLite WAL → fsync → close.
4. Closes the audit log → fsync → close.
5. `op signout --all` if `KEY_PROVIDER=1password`.
6. Process exits.

You can verify clean shutdown by checking the audit log doesn't have any open file descriptors:

```bash
lsof data/audit.log
# (empty — file is closed)
```

---

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| `Keystore passphrase rejected` at boot | Wrong passphrase | Re-run `wallet encrypt --force` to recreate (loses existing wallets — back up first) |
| `KEY_PROVIDER=encrypted_file requires KEYSTORE_FILE` | `.env` missing the path | `KEYSTORE_FILE=data/keystore.json` |
| `/send` → 503 `WALLET_POOL_EMPTY` | Boot loaded zero wallets | Check service log for the `WalletPool initialised` line; add a wallet via `wallet generate` |
| `/send` → 503 `WALLET_AUTOPICK_FAILED` | TronGrid is rate-limiting (free tier 429s) | Get a TronGrid API key; or pass explicit `wallet=NAME` |
| `429 RATE_LIMIT_EXCEEDED` | Hit per-IP limit (default 30/60s) | Bump `RATE_LIMIT_MAX` in `.env`; restart |
| `Idempotency store: IN-MEMORY` warning | `IDEMPOTENCY_DB_PATH` empty | Set in `.env` (wizard sets to `data/idempotency.db` by default) |
| Auto-shutdown after 10 min | `SHUTDOWN_TIMEOUT=600` (default) | Local dev — change to `0` in `.env` to disable |

---

## What's installed where

```
~/.skr-crypto/                    (or wherever you ran `skr-crypto install --dir`)
├── .env                          chmod 600, all settings
├── data/
│   ├── keystore.json             chmod 600, AES-256-GCM
│   ├── audit.log                 chmod 600, append-only JSON
│   ├── idempotency.db            SQLite WAL
│   └── ofac-sdn-trx.txt          OFAC SDN cache
└── venv/                         Python 3.12 + dependencies
```

`skr-crypto config show` prints the resolved configuration. `skr-crypto doctor` runs a battery of self-checks.

---

## Next steps

- [HTTP API reference](../api-reference.md) — every endpoint with curl examples
- [Multi-wallet pool](../multi-wallet.md) — managing more wallets, auto-pick semantics
- [Docker](docker.md) — same flow but containerised
- [VPS + systemd](systemd-vps.md) — production-grade single-host deploy
