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
| `KEY_PROVIDER` | `env` | One of `env` / `file` / `1password` / `keychain` |
| `OP_VAULT` | `Treasury` | (1password) vault name |
| `OP_ITEM` | `TRON-Treasury` | (1password) item name |
| `OP_FIELD` | `password` | (1password) field name |
| `PRIVATE_KEY_FILE` | (none) | (file) path to chmod-600 file with raw hex |
| `KEYCHAIN_SERVICE` | `payouts` | (keychain) macOS keychain `-s` |
| `KEYCHAIN_ACCOUNT` | `treasury` | (keychain) macOS keychain `-a` |

`PRIVATE_KEY_HEX` is read by the `env` provider — set it in the
process environment, **not** in `.env`. The CLI's `install` does
not write it to disk.

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
