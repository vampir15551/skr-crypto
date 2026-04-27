# Inspect commands

All read-only. None of these can move money, modify the audit log, or
mutate the idempotency store.

## `status`

```bash
skr-crypto status [--json] [--via REGIME]
```

Single screen: installed? running? which version? regime? uptime?
Hits `/api/v1/health/live` and `/api/v1/version` with a 3s timeout
each, so it's fast even when TronGrid is slow.

## `balance`

```bash
skr-crypto balance [--json]
```

Calls `/api/v1/balance` (which itself queries TronGrid). Expect ~1-3s
under good conditions; the request times out at 30s. Shows TRX, USDT,
energy available, bandwidth (free + staked), and TRON power staked.

## `check`

```bash
skr-crypto check <txid> [--json]
```

Look up a transaction hash on-chain. Returns one of:

| Status | Meaning |
|---|---|
| `SUCCESS` | landed and the contract call succeeded |
| `OUT_OF_ENERGY` | broadcast OK but on-chain reverted |
| `REVERT` | contract reverted (rules / blacklist / paused) |
| `NOT_FOUND` | no record on this RPC endpoint |
| `<other>` | any other Tron receipt code |

Implemented by running a small probe inside the service's venv —
counts against the `TRONGRID_API_KEY` rate limit.

## `audit`

```bash
skr-crypto audit [--today | --since DATE] [--event EVENT] [--to ADDR]
                 [--limit N] [--json]
```

Browse the durable audit log. Reads `data/audit.log` directly — no
HTTP round trip, works even when the service is down.

| Option | Default | Description |
|---|---|---|
| `--today` | off | Records since 00:00 MSK today (UTC+3) |
| `--since DATE` | (none) | Lower bound, ISO date or full timestamp |
| `--event EVENT` | any | `SEND_SUCCESS` / `SEND_FAILED` / `SEND_REJECTED` / `SEND_DUPLICATE` / `STARTUP_CHECK` |
| `--to ADDR` | any | Filter by recipient TRON address |
| `--limit N` | `50` | Max records shown (newest first) |
| `--json` | off | One JSON record per stdout line — pipe into `jq -c '...'` |

## `reconcile`

```bash
skr-crypto reconcile
```

Run the service's `startup_check.run_startup_check()` once without
restarting. Useful after a power cut, a flaky TronGrid window, or
just to spot-check a suspicious settle without taking the service
down. Streams the same `[STARTUP_CHECK]` lines the service emits at
boot.

## `version`

```bash
skr-crypto version [--json]
```

Prints both the CLI version (always known) and the running service's
version (from `/api/v1/version` if reachable). When the service isn't
running, the service line says `not running`.
