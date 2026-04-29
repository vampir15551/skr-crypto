# Runbook: TronGrid RPC outage

> **Symptom.** `[SEND] Estimate unavailable` lines, repeated retry log lines, requests timing out, or every `/send` returning `502 broadcast_failed`.
> **Cause.** TronGrid is returning 5xx, rate-limiting, or timing out the connection.
> **TL;DR fix.** First confirm it's TronGrid and not us; then rotate `TRONGRID_API_KEY` if rate-limited, or switch to a backup endpoint and `skr-crypto restart`.

The service is built to ride out short TronGrid blips on its own. A
sustained outage — more than a couple of minutes — is when an
operator has to step in.

## 1. Confirm it's actually TronGrid

Before touching anything, rule out the local machine.

```bash
# Direct probe of TronGrid from the host.
curl -sS --max-time 10 -o /dev/null -w "%{http_code}\n" \
    https://api.trongrid.io/wallet/getnowblock

# Same probe with the API key the service is using.
curl -sS --max-time 10 -o /dev/null -w "%{http_code}\n" \
    -H "TRON-PRO-API-KEY: $TRONGRID_API_KEY" \
    https://api.trongrid.io/wallet/getnowblock
```

If the unauthenticated probe returns 200 but the authenticated one
returns 401/403, your API key is dead — see step 4. If both return
5xx or hang, it's an actual TronGrid outage and you wait or fail
over.

Also check the public dashboard:

```bash
open "https://status.trongrid.io/"
```

## 2. Symptoms in service logs

When TronGrid is degraded, the log spells it out:

```
[SEND] Estimate unavailable  reason=trongrid_5xx attempt=1
[SEND] Estimate unavailable  reason=trongrid_5xx attempt=2
[SEND] Broadcast retry attempt=2
[BALANCE] RPC error  status=503
[HEALTH] Trongrid probe failed
```

The `attempt=N` counter is the retry number. Three of those for a
single request means the retry budget is exhausted and the request
is about to return `502 broadcast_failed`. Watch how often that
happens — a few per minute is noise, dozens per minute is an outage.

## 3. What automatic behaviour kicks in

The service does several things on its own before requiring an
operator:

- **Retry with backoff** on idempotent reads (`getaccount`,
  `triggerconstantcontract`) up to 3 attempts with exponential
  backoff and jitter. `broadcasttransaction` is **not retried**.
- **Energy-estimate fallback.** If `triggerconstantcontract` returns
  "this node does not support estimate energy" (common on the free
  tier), the service falls back to the `USDT_FEE_LIMIT_SUN` ceiling
  and continues. That's documented in the OPERATIONS configuration
  reference.
- **Rate-limit headers respected.** If TronGrid responds with
  `429 Too Many Requests`, the service backs off the configured
  retry interval before re-attempting.

Most short outages (sub-minute) are absorbed by these. If you see
sustained errors past that, manual intervention starts.

## 4. Rotate the API key (rate-limited or revoked)

If you confirmed the key is dead — 401/403 from the authenticated
probe, or rate-limit warnings in the dashboard — rotate it.

```bash
# Generate a new key in the TronGrid dashboard, then:
$EDITOR /etc/skr-crypto/.env       # update TRONGRID_API_KEY=...
skr-crypto restart
```

After restart, watch for `[BOOT] Trongrid probe ok` in the log. If
that line is absent and you see the same 401, the new key did not
take — confirm `.env` permissions (`chmod 600`) and the running
process's environment matches.

## 5. Switch to a backup endpoint

If TronGrid is genuinely down (status page is red, both probes
failing), point the service at an alternate provider. Edit `.env`:

```bash
$EDITOR /etc/skr-crypto/.env       # update TRONGRID_BASE_URL=...
skr-crypto restart
```

Known alternatives (in rough order of operator preference):

- A different TronGrid region — change the `BASE_URL` to e.g.
  `https://api.shasta.trongrid.io` (testnet) or a regional endpoint
  if your plan provides one.
- A self-hosted Java-tron node, if the operator has one available.
  Set `TRONGRID_BASE_URL` to the node's HTTP fullnode port.
- A third-party RPC (NowNodes, GetBlock, QuickNode). Ensure the API
  shape matches TronGrid's — these are usually compatible but
  always smoke-test first.

After the restart, the service will log `[BOOT] Trongrid probe ok`
against the new endpoint. If it doesn't, the URL is wrong or the
provider's auth scheme doesn't match what the service expects;
revert `.env` and try a different one.

## 6. Verify recovery

Once the endpoint is healthy, confirm it end-to-end before declaring
the incident over:

```bash
# Treasury balance round-trip (hits TronGrid).
skr-crypto balance

# Re-verify a known-good recent txid.
skr-crypto check <recent_txid>

# Health endpoint with full RPC.
curl -sS -H "X-API-Key: $AUTH_TOKEN" https://<host>/api/v1/health | jq
```

`balance` returning a non-zero TRX/USDT amount and `check` returning
`SUCCESS` for a known-good txid means the RPC path is fully back.
The next `/send` will go through normally.

## 7. When NOT to switch endpoints

A short blip — one or two failed requests, recovers within a minute
— is not worth a restart. Restarts have their own cost: idempotency
state survives but in-flight requests are killed mid-broadcast and
end up `UNRESOLVED`, which then needs the
[`unresolved-key.md`](unresolved-key.md) playbook. The retry budget
exists exactly so you don't have to reach for the restart button on
every flake.

Reach for it when: status page is red, multiple consecutive minutes
of failures, or rate-limit alerts fired. Otherwise let it ride.

## See also

- [`OPERATIONS.md`](../OPERATIONS.md) — operations index
- [`stuck-broadcast.md`](stuck-broadcast.md) — when a single request
  is hanging rather than the whole service
- [`SECURITY.md`](../SECURITY.md#cryptographic-posture) — note that
  TronGrid is a trust boundary; we trust it to report honestly
