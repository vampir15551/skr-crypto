# Configuration reference

Two things are configurable:

- **The CLI itself** — at most an install dir, via `--dir`,
  `$SKR_CRYPTO_HOME`, or the default `~/.skr-crypto`.
- **The service** — its `.env`, written into the install dir by
  `skr-crypto install`. Edit with `skr-crypto config edit`. The CLI
  reads it; the service reads it; nobody mutates it without a restart.

The wizard (`skr-crypto install` non-interactive, or
`skr-crypto install --interactive`) sets sensible defaults. Edit by
hand only when you need to.

## `.env` — every variable

### Auth

| Variable | Default | Notes |
|---|---|---|
| `AUTH_TOKEN` | (auto-generated on `install`) | API key for `X-API-Key`. 32+ chars; doctor warns if shorter. |

### Key provider

| Variable | Default | Notes |
|---|---|---|
| `KEY_PROVIDER` | `1password` | One of `env` / `file` / `1password` / `keychain` / `encrypted_file` |
| `WALLETS` | (empty) | Comma-separated wallet names. Empty → single-wallet legacy globals below. |
| `OP_VAULT` | `Treasury` | (1password) vault name; shared across multi-wallet |
| `OP_ITEM` | `TRON-Treasury` | (1password) item name (single-wallet legacy) |
| `OP_FIELD` | `password` | (1password) field name |
| `PRIVATE_KEY_FILE` | (none) | (file) path to chmod-600 file with raw hex (single-wallet legacy) |
| `KEYCHAIN_SERVICE` | `skr-crypto` | (keychain) macOS keychain `-s`; shared across multi-wallet |
| `KEYCHAIN_ACCOUNT` | `treasury` | (keychain) macOS keychain `-a` (single-wallet legacy) |
| `KEYSTORE_FILE` | (none) | (encrypted_file) path to chmod-600 AES-256-GCM JSON |
| `KEY_PASSPHRASE` | (none) | (encrypted_file) env-supplied passphrase. Consumed + cleared from `os.environ`. |
| `KEY_PASSPHRASE_FILE` | (none) | (encrypted_file) chmod-600 file with the passphrase. Used when `KEY_PASSPHRASE` empty. |

For multi-wallet (`WALLETS` set) the per-wallet variables are:

| Provider | Per-wallet variable |
|---|---|
| `env` | `WALLET_<NAME>_PRIVATE_KEY_HEX` |
| `file` | `WALLET_<NAME>_PRIVATE_KEY_FILE` |
| `1password` | `WALLET_<NAME>_OP_ITEM` |
| `keychain` | `WALLET_<NAME>_KEYCHAIN_ACCOUNT` |
| `encrypted_file` | n/a — keystore file is the source of truth |

`PRIVATE_KEY_HEX` (single-wallet) and `WALLET_<NAME>_PRIVATE_KEY_HEX` (multi-wallet) are read by the `env` provider — set them in the **process environment**, not in `.env`. The CLI's `install` does not write them to disk.

See [Key providers](key-providers.md) for the full backend comparison and [Multi-wallet pool](multi-wallet.md) for `WALLETS=` semantics.

### TRON network

| Variable | Default | Notes |
|---|---|---|
| `TRON_NETWORK` | `mainnet` | `mainnet` / `shasta` / `nile` |
| `TRONGRID_API_KEY` | (none) | Strongly recommended — free tier rate-limits hit fast |
| `USDT_CONTRACT` | mainnet USDT | Override only for testnets |
| `USDT_FEE_LIMIT_SUN` | `30000000` | Per-tx fee_limit ceiling (30 TRX) |
| `MIN_TRX_RESERVE` | `50` | Refuse `/send` if treasury TRX < this |
| `TRON_HTTP_TIMEOUT` | `15` | Per-RPC timeout (seconds) |

### Cost / energy gates

| Variable | Default | Notes |
|---|---|---|
| `TRON_ENERGY_PRICE_SUN_FALLBACK` | `420` | Used if chain query fails |
| `MAX_ENERGY_BURN_TRX` | `20` | Reject `/send` if estimate > this. `0` disables. |
| `FEE_LIMIT_SAFETY_MULT` | `1.3` | Headroom multiplier on the energy estimate |

### Recipient risk preflight

| Variable | Default | Notes |
|---|---|---|
| `RISK_BLOCK_LEVEL` | `high` | Block `/send` at this level. `none` / `medium` / `high`. |
| `RISK_USE_EXTERNAL` | `true` | Query TronScan reputation (~+300ms per /send) |
| `MISTTRACK_API_KEY` | (none) | Opt in to MistTrack AML (`high` on score ≥ 60). Free tier ~100/day. |
| `SANCTIONS_LIST_URL` | OFAC TRX list on GitHub | Source of OFAC SDN list |
| `SANCTIONS_LIST_REFRESH` | `true` | Fetch on every boot. `false` = use on-disk cache only (air-gapped). |

See [Risk preflight](risk-preflight.md) for the full check matrix.

### Persistence

| Variable | Default | Notes |
|---|---|---|
| `AUDIT_LOG_FILE` | `data/audit.log` | Durable audit. Empty = stdout-only (NOT durable). |
| `IDEMPOTENCY_DB_PATH` | `data/idempotency.db` | SQLite WAL store. Empty = in-memory (lost on restart). |

Relative paths resolve from the install dir. Use absolute paths if you
mount data on a separate volume.

### Server

| Variable | Default | Notes |
|---|---|---|
| `SERVER_HOST` | `127.0.0.1` | Bind address |
| `SERVER_PORT` | `8000` | TCP port |
| `SHUTDOWN_TIMEOUT` | `600` | Idle seconds before auto-shutdown. Set very high under systemd / Docker. |

### Rate limiting

| Variable | Default | Notes |
|---|---|---|
| `RATE_LIMIT_MAX` | `30` | Max requests per `RATE_LIMIT_WINDOW` per IP |
| `RATE_LIMIT_WINDOW` | `60` | Seconds |
| `TRUSTED_PROXIES` | (none) | Comma-separated IPs that may set `X-Forwarded-For` |

## CLI environment variables

Apart from the above (which are the service's), the CLI itself reads:

| Variable | Notes |
|---|---|
| `SKR_CRYPTO_HOME` | Install dir override (lowest priority — `--dir` wins) |
| `EDITOR` / `VISUAL` | Used by `config edit`. Falls back to `nano` / `vim` / `vi`. |

## Re-applying a config change

The service reads env vars **once at startup**. Editing `.env`
doesn't take effect until restart:

```bash
skr-crypto config edit
skr-crypto restart
skr-crypto status      # confirm the new uptime resets
```
