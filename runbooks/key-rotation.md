# Runbook: TRON treasury private key rotation

> **Symptom.** Suspected key leak — `.env` was committed by mistake, a backup ended up somewhere it shouldn't, a process dump went to a paste site, a laptop with the key was lost. Or just routine rotation.
> **Cause.** The current treasury private key is no longer trusted.
> **TL;DR fix.** Generate a new key, fund the new address, **drain USDT from old to new manually via TronLink/tronscan** (the CLI cannot move money), update `KEY_PROVIDER` target, restart, verify.

This is a treasury operation, not a configuration change. Take it
seriously: every minute the compromised key is on a funded address
is a minute an attacker can drain it. If this is a leak rather than
routine rotation, work fast.

## The "this CLI cannot move money" rule

`skr-crypto` is read-only on the money path by design. There is no
`skr-crypto send`, no `skr-crypto sweep`, no `skr-crypto drain`.
This is structural — see [`GUIDELINES.md`](../GUIDELINES.md) § "No
money path in the CLI" — and there is no override.

**You drain USDT from the old address using a wallet you control.**
TronLink is the standard choice; the operator has the seed and can
import the old key into a fresh wallet for the duration of the
drain. Tronscan's "Send" action plus a hardware wallet works too.
The service plays no role in the drain; it does not get to be
involved in moving funds it has signed for in the past.

## 1. Generate the new key

```bash
skr-crypto keygen --write-to <provider>     # 1password|keychain|file|env
skr-crypto keygen --address-only --provider <provider>
```

`--write-to` writes the key into the chosen backend without
printing it to stdout. `--address-only` prints only the address —
**never paste the key itself anywhere**.

## 2. Fund the new address with TRX

The new address is empty. Send a starter amount (50-100 TRX is
typical — covers a dozen-ish transfers plus the `MIN_TRX_RESERVE`
floor) from an exchange, faucet, or other treasury. Confirm:

```bash
curl -sS "https://api.trongrid.io/v1/accounts/<NEW_ADDR>" | jq '.data[0].balance'
# value is in sun (1 TRX = 1_000_000 sun)
```

## 3. Drain USDT from the old address to the new one

**This step happens outside `skr-crypto`.** Using TronLink:

1. `skr-crypto stop` — lock the service so no further `/send`
   succeeds on the old key.
2. Open TronLink. Import the **old** treasury key as a temporary
   wallet (or use an existing copy).
3. Send the full USDT balance from old → new. Double-check the
   destination character by character; there is no undo.
4. Send residual TRX you don't want stranded.
5. Once tronscan confirms the transfers (5-30s), close TronLink
   and wipe the imported wallet. **Do not leave the old key in
   any wallet that stays online.**

Verify on-chain at
`https://tronscan.org/#/address/<OLD_ADDR>/transactions` — old
USDT balance should be zero, new should equal what was on the old.

## 4. Update the `KEY_PROVIDER` target

The new key is already written from step 1; the service just has
to be told to load it. Confirm the address in the chosen backend
matches the new one (use the right command for your provider):

```bash
# 1password
op item get "$OP_ITEM" --vault "$OP_VAULT" --fields address

# file
stat -f '%A %u' "$KEY_FILE_PATH"   # macOS — expect 600 + service user
stat -c '%a %U' "$KEY_FILE_PATH"   # linux

# keychain (macOS only)
security find-generic-password -a skr-crypto -s tron-treasury

# env
grep '^TRON_PRIVATE_KEY=' /etc/skr-crypto/.env | cut -c1-30
# only the prefix — never paste the full value
```

## 5. Restart the service

```bash
skr-crypto restart
```

Watch the boot log:

```
[BOOT] KeyProvider load ok provider=...
[BOOT] Treasury address=<NEW_ADDR>
[BOOT] Trongrid probe ok
[BOOT] Listening on 127.0.0.1:8000
```

If the treasury address printed at boot does not match the new
address, **stop immediately**. Either keygen wrote to the wrong
backend or the env var is pointing at the old one. Fix it before
sending traffic.

## 6. Verify

```bash
# Confirms the service can read the chain with the new key.
skr-crypto balance

# Round-trip: a real /send to a known recipient, small amount.
# (Use an internal test recipient, never an external one for this.)
curl -sS -X POST https://<host>/api/v1/send \
    -H "X-API-Key: $AUTH_TOKEN" \
    -H "Idempotency-Key: rotation-test-$(date +%s)" \
    -H "Content-Type: application/json" \
    -d '{"to": "<TEST_RECIPIENT>", "amount": "0.1"}' | jq
```

The response should include a txid. Look it up on tronscan and
confirm the `from` field is the new address.

## 7. Aftercare

- **Update the AUTH_TOKEN too**, if the leak might have included it.
  See [`SECURITY.md`](../SECURITY.md#what-you-must-rotate-after-a-suspected-leak)
  for the full post-leak rotation order — private key first,
  AUTH_TOKEN second, TRONGRID_API_KEY third.
- **Preserve the audit log of the old address.** Don't delete
  `audit.log`. The old transactions are still legitimate history
  even though the key is retired.
- **Post-mortem.** Write down what leaked, how it leaked, what you
  changed to prevent recurrence. Drop it in an internal channel —
  not the public repo.

## See also

- [`OPERATIONS.md`](../OPERATIONS.md) — operations index
- [`SECURITY.md`](../SECURITY.md#what-you-must-rotate-after-a-suspected-leak)
  — full leak-rotation procedure (key + token + API key)
- [`GUIDELINES.md`](../GUIDELINES.md) § "No money path in the CLI"
- [ADR 0002](../docs/adr/0002-pluggable-key-providers.md) —
  KeyProvider backends
