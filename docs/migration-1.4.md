# Migrating to 1.4

The 1.4.0 release introduced multi-wallet routing as a hard cut. Wire formats, internal APIs, and the audit record shape all changed. There's no compat shim — call sites should be updated, not papered over.

This page is the upgrade playbook.

---

## TL;DR

If you only have one wallet and you don't care about the new fields, you don't have to do anything except:

1. Upgrade the package.
2. Restart the service.
3. Optionally migrate to `KEY_PROVIDER=encrypted_file` for multi-wallet readiness.

The single-wallet legacy globals (`PRIVATE_KEY_HEX`, `PRIVATE_KEY_FILE`, `OP_ITEM`, `KEYCHAIN_ACCOUNT`) keep working unchanged. Your existing `/send` payloads keep working — the new `wallet` field is optional.

If you have a typed client that pinned the response schema, see "Response shape changes" below.

---

## What changed at the wire level

### `POST /api/v1/send`

**Request** — adds optional `wallet`:
```json
{
  "to_address": "...",
  "amount": "...",
  "idempotency_key": "...",
  "wallet": "cold"           ← new in 1.4.0, optional
}
```

**Response** — adds `wallet`:
```diff
{
  "txid": "...",
  "from_address": "...",
+ "wallet": "default",
  "to_address": "...",
  "amount": "...",
  "idempotency_key": "...",
  "status": "broadcast"
}
```

**Errors** — three new codes:
- `404 WALLET_NOT_FOUND` (caller specified an unknown name)
- `503 WALLET_POOL_EMPTY` (boot loaded zero wallets — config error)
- `503 WALLET_AUTOPICK_FAILED` (every TronGrid balance lookup failed)

### `GET /api/v1/balance`

**Request** — adds optional `?wallet=NAME` query.

**Response** — adds `wallet`:
```diff
{
+ "wallet": "default",
  "address": "...",
  "trx": "...",
  "usdt": "...",
  ...
}
```

### `GET /api/v1/health`

**Response** — drops the per-wallet snapshot, adds wallet-pool stats:

```diff
{
  "status": "ok",
- "address": "...",
  "network": "mainnet",
  "uptime_seconds": ...,
  "shutdown_in_seconds": ...,
  "node_connected": true,
- "energy_available": ...,
- "bandwidth_free_available": ...,
- "bandwidth_paid_available": ...,
- "tron_power_staked": ...,
+ "wallet_count": 1,
+ "wallet_names": ["default"]
}
```

The per-wallet resource fields moved to `/api/v1/balance` (which now has a `?wallet=` parameter) and a new `/api/v1/wallets` endpoint.

### NEW: `GET /api/v1/wallets`

Lists every configured wallet with live balances + the auto-pick winner. Authenticated.

```json
{
  "wallets": [
    {"wallet": "main", "address": "...", "trx": "...", "usdt": "...", ...},
    ...
  ],
  "auto_pick": "main"
}
```

### Audit record shape

Every `SEND_*` event now carries a `wallet` field:

```diff
{
  "event": "SEND_SUCCESS",
+ "wallet": "default",
  "from_address": "...",
  ...
}
```

Pre-1.4 records do not have this field. Aggregation queries should treat missing `wallet` as `default` for backward compat.

---

## What changed at the internal Python API

This matters only if you wrote code that imports `skr_crypto.server.*` directly (most operators don't).

### `TronClient` is keyless

```python
# 1.3.x
tron.address                    # string
tron.priv_key                   # PrivateKey
tron.get_trx_balance()          # for the singleton wallet
tron.get_usdt_balance()
tron.send_usdt(to, amount, fee_limit_sun=...)
tron._get_usdt_contract()       # private

# 1.4.0
# tron has no address or priv_key.
tron.get_trx_balance_for(addr)              # any address
tron.get_usdt_balance_for(addr)
tron.get_resource_summary_for(addr)
tron.estimate_transfer_energy(from_addr, to_addr, amount)
tron.get_usdt_contract()                    # public

# Signing now lives on Wallet:
from skr_crypto.server.wallet_pool import wallets
wallet = wallets.resolve(name)              # or None for auto-pick
wallet.send_usdt(tron.client, tron.get_usdt_contract(), to, amount, fee_limit_sun=...)
```

### `security.load_private_key()` → `load_wallets()`

```python
# 1.3.x
key = load_private_key()
priv = PrivateKey(bytes(key))

# 1.4.0
wallets_list = load_wallets()        # list[Wallet]
for w in wallets_list:
    w.address, w.priv_key             # already-built
```

### `KeyProvider` interface

```python
# 1.3.x
class KeyProvider(Protocol):
    def get_private_key(self) -> bytearray: ...
    def lock(self) -> None: ...

# 1.4.0
class KeyProvider(Protocol):
    def load_wallets(self) -> list[LoadedWallet]: ...
    def lock(self) -> None: ...
```

`LoadedWallet` is a dataclass with `name: str`, `raw_key: bytearray`, `address: str` (best-effort hint, may be empty).

---

## Migration paths

### Path 1: Stay single-wallet, just upgrade the package

You don't change anything in `.env`. You don't change your caller. You upgrade and restart.

```bash
pip install --upgrade 'skr-crypto[server]'
skr-crypto restart   # or systemctl restart skr-crypto
```

The legacy globals load as a wallet named `default`. Every `SEND_*` audit record will carry `wallet=default`. Done.

### Path 2: Keep the same backend, opt into multi-wallet

Pick env / file / 1password / keychain depending on what you already have. Add per-wallet env vars and the `WALLETS=` list:

#### env

```bash
# Was:
PRIVATE_KEY_HEX=<old-hex>

# Becomes:
WALLETS=main,reserve
WALLET_MAIN_PRIVATE_KEY_HEX=<old-hex>            # same key as before
WALLET_RESERVE_PRIVATE_KEY_HEX=<new-hex>         # new wallet
# remove PRIVATE_KEY_HEX
```

#### file

```bash
# Was:
PRIVATE_KEY_FILE=/etc/skr-crypto/treasury.key

# Becomes:
WALLETS=main,reserve
WALLET_MAIN_PRIVATE_KEY_FILE=/etc/skr-crypto/treasury.key
WALLET_RESERVE_PRIVATE_KEY_FILE=/etc/skr-crypto/reserve.key
# remove PRIVATE_KEY_FILE
```

#### 1password

```bash
# Was:
OP_ITEM=TRON-Treasury

# Becomes:
WALLETS=main,reserve
WALLET_MAIN_OP_ITEM=TRON-Treasury
WALLET_RESERVE_OP_ITEM=TRON-Reserve
# OP_VAULT and OP_FIELD stay shared
# remove OP_ITEM
```

#### keychain

```bash
# Was:
KEYCHAIN_ACCOUNT=treasury

# Becomes:
WALLETS=main,reserve
WALLET_MAIN_KEYCHAIN_ACCOUNT=treasury
WALLET_RESERVE_KEYCHAIN_ACCOUNT=reserve
# KEYCHAIN_SERVICE stays shared
# remove KEYCHAIN_ACCOUNT
```

Restart the service. `skr-crypto wallet list` should show both.

### Path 3: Migrate to `encrypted_file` (recommended for new installs)

This is the cleanest multi-wallet experience. One file, one passphrase, container-friendly.

```bash
# 1. Take the service offline (or leave it running on the old config; encryption happens out-of-band).
systemctl stop skr-crypto                              # or your equivalent

# 2. Initialise the encrypted keystore. Prompts for a fresh passphrase.
skr-crypto wallet encrypt -o data/keystore.json

# 3. Import existing keys into the keystore.
# If you had KEY_PROVIDER=env with PRIVATE_KEY_HEX:
PRIVATE_KEY_HEX=<your-existing-hex> skr-crypto wallet encrypt \
    -o data/keystore.json --force --from-env PRIVATE_KEY_HEX --name main

# Or if you had KEY_PROVIDER=file:
HEX=$(cat /etc/skr-crypto/treasury.key)
skr-crypto wallet add main --hex "$HEX" --yes

# Add more wallets while you're here:
skr-crypto wallet generate cold

# 4. Update .env:
#    KEY_PROVIDER=encrypted_file
#    KEYSTORE_FILE=data/keystore.json
#    KEY_PASSPHRASE=<from-secret-manager>   # or KEY_PASSPHRASE_FILE=...
#    Comment out or remove PRIVATE_KEY_HEX / PRIVATE_KEY_FILE / OP_ITEM / etc.

# 5. Restart.
systemctl start skr-crypto

# 6. Verify:
curl -H "X-API-Key: $AUTH_TOKEN" http://127.0.0.1:8000/api/v1/wallets
```

Once the encrypted_file backend is loading the same keys cleanly, you can delete the originals (the file at `PRIVATE_KEY_FILE`, the env var, the 1Password item) — but keep at least one offline backup of the hex somewhere safe before doing so.

---

## Caller code update checklist

For each caller that hits the API:

- [ ] If you read `from_address` from `/send` responses, decide whether you also want to record `wallet` (recommended for multi-wallet ops).
- [ ] If you're explicitly multi-wallet, start sending `"wallet": "..."` in `/send` bodies. Otherwise let auto-pick handle it.
- [ ] If you parse `/health`, replace `address` / `energy_available` / `bandwidth_*_available` / `tron_power_staked` reads with calls to `/balance` (or `/wallets` for fleet view). The `/health` endpoint is now wallet-pool-shaped.
- [ ] If your retry logic catches HTTP errors by status code, add handlers for new 404 / 503 codes (`WALLET_NOT_FOUND`, `WALLET_POOL_EMPTY`, `WALLET_AUTOPICK_FAILED`). Treat 404 as "fix your config", 503 as "retry".
- [ ] If you log /send responses, add `wallet` to your structured log fields.

---

## Test the upgrade safely

If your topology allows, do a green-field test:

1. Spin up a 1.4 service on a new staging host with the **same key** (export from your prod store, import into the staging keystore).
2. Run a smoke script: `/health/live`, `/health`, `/balance`, `/risk/<known-good-addr>`, `/send` with a tiny amount and a unique idempotency key.
3. Re-run the same `/send` — must return `status: "duplicate"` with the same txid.
4. Test multi-wallet: `wallet generate cold`, restart, `/wallets` must show both, `/send` with `"wallet": "cold"` must use cold.
5. Tail audit and verify every record has the `wallet` field.

When the staging path is clean, repeat on prod with traffic paused.

---

## Rollback

Going back to 1.3.x from a fresh 1.4 deploy is straightforward:

- The audit log is forward-compatible (1.3.x will simply ignore the `wallet` field — it doesn't validate the JSON shape on read).
- The idempotency DB schema is unchanged.
- The keystore file (encrypted_file provider) **is not readable** by 1.3.x — that backend doesn't exist there. You must switch back to env / file / 1password / keychain before downgrading. Export the keys from the keystore (use a 1.4 binary's `wallet add` reverse — there's no `wallet export` yet, but `load_keystore` from a Python REPL works) and re-populate the legacy globals.
- The `/send` payloads with `"wallet"` field are silently ignored by 1.3.x (Pydantic's default is `extra = ignore`).

If you migrated *to* 1.4 and want to roll back, the path is symmetric — just understand that you lose multi-wallet on the way back.

---

## See also

- [HTTP API reference](api-reference.md) — every endpoint with examples
- [Multi-wallet pool](multi-wallet.md) — the new model end-to-end
- [Key providers](key-providers.md) — the five backends in detail
- [Encrypted keystore format](keystore-format.md) — what the new file looks like
- [Changelog](changelog.md) — full 1.4.0 entry
