# 0005 — Multi-wallet routing via WalletPool with auto-pick by max USDT

## Status

Accepted. 2026-04-30. Introduced in 1.4.0.

## Context

Until 1.3.x, the service held a single private key. The code reflected that: `tron.address`, `tron.priv_key`, `tron.send_usdt(to, amount, ...)`. Every endpoint that touched a wallet implicitly meant *the* wallet.

Operationally, that one-key shape kept causing problems:

- **Hot/cold separation.** Operators wanted a small "hot" balance for routine payouts and a larger "cold" reserve they topped up from. With one key, every payout drained from the same balance — accidental large transfers had nothing to gate them, and refilling required moving funds out-of-band.
- **Per-customer or per-jurisdiction wallets.** A few operators wanted distinct source addresses for distinct cohorts of recipients (regulatory hygiene, simpler reporting). The single-wallet shape forced them to run multiple service instances, each with its own port + audit + idempotency DB.
- **Dev/test isolation.** Testing a new flow on testnet without polluting the production audit required a second instance.

The obvious fix is "support N wallets". The non-obvious questions:

1. **What does a request without an explicit wallet do?** Reject? Fail open? Pick one?
2. **Where does the pool live?** A new singleton, or split the existing tron singleton?
3. **How are the keys configured?** Same provider abstraction, or per-wallet?
4. **What's the wire format change for existing callers?**

## Decision

Add a process-wide `WalletPool` that holds N `Wallet` objects, each carrying name + address + PrivateKey. Refactor `TronClient` into a keyless RPC layer; move signing from `tron.send_usdt` to `Wallet.send_usdt`. Make `wallet` an optional parameter on every endpoint that names a wallet, with these resolution semantics:

- **Name given** → exact lookup; 404 `WALLET_NOT_FOUND` on miss.
- **Name omitted, single wallet in pool** → return that wallet (preserves the legacy shape).
- **Name omitted, multiple wallets** → live USDT-balance lookup per wallet; return the wallet with the highest balance. 503 `WALLET_AUTOPICK_FAILED` if every lookup raises.

Each existing key provider (`env`, `file`, `1password`, `keychain`) gains a `WALLETS=name1,name2,…` mode with per-wallet env vars. The single-wallet legacy globals keep working — they load as a wallet named `default`. A fifth provider, `encrypted_file`, is multi-wallet by construction (see ADR 0006).

The audit log gains a `wallet` field on every `SEND_*` event. The `[SEND] SUCCESS` log line includes wallet + risk verdict together so post-mortem grep is one line.

`/api/v1/balance` accepts `?wallet=NAME`; same resolution rules. A new `/api/v1/wallets` lists every configured wallet with live balances and the auto-pick winner.

## Consequences

The single-wallet shape is **gone** from the codebase. There's no compat shim. Existing callers that omit `wallet` keep working unchanged because the resolution rules degenerate cleanly for `count == 1`.

`/api/v1/health` no longer carries per-wallet resource fields (which wallet would they belong to?). Resources moved to `/api/v1/balance` (which now takes `?wallet=`) and `/api/v1/wallets`. This is a wire-format break for any caller that pinned the `/health` schema.

Auto-pick costs N TronGrid RPCs per `/send` when `count > 1`. Operators who care about per-request latency pass `wallet=NAME` explicitly. We do not cache auto-pick winners — the cache would either be stale (and silently send from the wrong wallet) or invalidated on every `/send` (defeating the point).

The blast radius of a multi-wallet `/send` failure is bounded by the chosen wallet — `WALLET_NOT_FOUND` is 404 (caller fixes the name), `WALLET_POOL_EMPTY` is 503 (operator fixes config), `WALLET_AUTOPICK_FAILED` is 503 (transient, retry). None are 500 — the service is up, the request just can't be processed.

The `Wallet` class becomes the only place that calls `.sign()`. Any signing-related change, including any future hardware-wallet integration, is bounded to one file.

## Alternatives considered

- **Multiple service instances, one wallet each.** This was the workaround in 1.3.x. Drawbacks: N times the boot overhead, N audit logs to merge for forensics, N idempotency DBs (or a shared one with prefix collisions), N healthchecks. Operationally awful.
- **Implicit "first wallet" instead of auto-pick.** Pick the alphabetically-first wallet's name when none specified. Trivial but useless — you can't add a "more important" wallet later without renaming all the existing ones.
- **Round-robin auto-pick.** Distribute load across wallets. Rejected because it would routinely drain the wrong wallet first; operators want the *largest* balance to handle payouts so the smallest balance stays available as a buffer.
- **Pick by remaining TRX (energy budget) instead of USDT.** Considered, but energy availability changes faster than USDT and would lead to ping-ponging between wallets within a minute. USDT is the slowly-moving signal that matches the operator's mental model.
- **Cache the auto-pick winner for a TTL (60 s).** Rejected — the failure mode where two `/send`s happen 30s apart and both go to the now-emptied wallet because the cache lied is exactly the failure we want to avoid. The cost of N RPCs per `/send` is acceptable given our /send rate.
- **Make the wallet a path parameter (`/api/v1/wallet/cold/send`).** Cleaner REST, but breaks every existing caller's URL. The optional-field approach degrades gracefully for single-wallet deploys.
- **Hot-rotate wallets without restart.** Tempting, but the lock-free reads on the `WalletPool` dict assume it's frozen after `init()`. Adding hot-rotation means an RLock around every `pool.get()` and a coordination protocol with in-flight `/send`s. Restart on rotation is fine for now; we can add hot-rotation in a future ADR if we ever need it.

## Related

- [Multi-wallet pool](../multi-wallet.md) — operator-facing reference.
- [HTTP API: /send](../api-reference.md#post-apiv1send) — the wire format.
- [ADR 0002](0002-pluggable-key-providers.md) — the provider abstraction this builds on.
- [ADR 0006](0006-encrypted-keystore.md) — the encrypted_file backend that ships alongside this change.
