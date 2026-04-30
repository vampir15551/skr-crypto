# `skr-crypto wallet`

Manage the multi-wallet pool. Subcommands let you list configured wallets, add new ones (existing or freshly-generated), remove them, rename them, and initialise an encrypted keystore from scratch.

```text
skr-crypto wallet
├── list [--offline] [--json]
├── show NAME [--json]
├── add NAME [--hex HEX] [--yes]
├── generate NAME [--yes]
├── remove NAME [--yes]
├── rename OLD NEW
└── encrypt [--from-hex HEX | --from-env VAR] [-o PATH] [--name NAME] [--force]
```

For all commands, the install dir is taken from `--dir` / `SKR_CRYPTO_HOME` / the default `~/.skr-crypto`.

**A note on which provider you're using:** `wallet add`, `wallet generate`, `wallet remove`, `wallet rename`, and `wallet encrypt` modify state directly only for `KEY_PROVIDER=encrypted_file`. For `env`/`file`/`1password`/`keychain`, the commands print the exact env-var / file-path layout you must apply manually — the CLI never tries to edit external secret stores on your behalf.

---

## `wallet list`

Lists every configured wallet. Hits `/api/v1/wallets` when the service is reachable; falls back to a config-only summary when it isn't.

```bash
$ skr-crypto wallet list
┏━━━━━━━━━━━━━━━━━━━━━━ Wallets (auto-pick: cold) ━━━━━━━━━━━━━━━━━━━━━━┓
┃ Wallet  Address                              USDT      TRX    Energy  ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ cold    TColdxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx   12500.00  85.21  0       │
│ hot     THotxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx     250.00  41.00  6500     │
└───────────────────────────────────────────────────────────────────────┘
```

Flags:

- `--offline` — skip the live `/wallets` call. Useful when the service is down or for fast `list` invocations that don't need balance polling.
- `--json` — machine-readable output.

For `KEY_PROVIDER=encrypted_file`, the offline listing reads names directly from the keystore (no passphrase needed — names and addresses are stored unencrypted as hints). For other providers, it parses `.env`'s `WALLETS=` list.

---

## `wallet show NAME`

Show one wallet's full balance + on-chain resources. Equivalent to `skr-crypto balance --wallet NAME` with extra structure.

```bash
$ skr-crypto wallet show cold
┏━━━━━━━━━━━━━━━ Wallet cold ━━━━━━━━━━━━━━━┓
┃ Field                       Value         ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ wallet                      cold          │
│ address                     TColdxxxx...  │
│ trx                         85.21         │
│ usdt                        12500.00      │
│ energy_available            0             │
│ bandwidth_free_available    600           │
│ bandwidth_paid_available    0             │
│ tron_power_staked           0             │
└───────────────────────────────────────────┘
```

Requires the service to be running (this is a `/balance?wallet=NAME` call).

---

## `wallet add NAME`

Add an existing private key as wallet `NAME`.

```bash
$ skr-crypto wallet add cold --hex 1111111111...22
Derived address: TColdxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Add wallet 'cold'? [Y/n]: y
```

If `--hex` is omitted, prompted via `getpass` (no echo). Validates that the value is 64 hex chars and decodes to a non-zero 32-byte key.

For `KEY_PROVIDER=encrypted_file`:
- Decrypts the existing keystore (asks for passphrase).
- Adds the new entry under a fresh IV.
- Re-encrypts and atomically writes.
- Prints "Restart the service for the new wallet to become signable."

For other providers, prints the exact env-var / file-path layout. Example for `KEY_PROVIDER=env`:

```bash
$ skr-crypto wallet add reserve --hex 11...22
Address: TRsvxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Add wallet 'reserve'? [Y/n]: y
Address: TRsvxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export WALLET_RESERVE_PRIVATE_KEY_HEX=11...22
ℹ Append `reserve` to WALLETS in .env (comma-separated), then restart.
```

Apply the suggested env-var change manually, restart the service, the new wallet is loaded.

Flags:

- `--hex HEX` — provide the key inline. Prompts via `getpass` if omitted.
- `--yes` / `-y` — skip the "Add wallet 'NAME'?" confirmation.

---

## `wallet generate NAME`

Generate a fresh random TRON private key and add it as wallet `NAME`. Same machinery as `add`, but the key comes from `tronpy.PrivateKey.random()`.

```bash
$ skr-crypto wallet generate cold
This generates a fresh TRON private key. It is shown / persisted exactly once.
Generate now? [y/N]: y
Address: TColdxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Keystore passphrase: ********
✓ Added wallet 'cold' → TColdxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ℹ Restart the service for the new wallet to become signable: skr-crypto restart
```

For `KEY_PROVIDER=encrypted_file` the key is encrypted into the keystore directly. For other providers, the hex is printed and you must apply the suggested layout.

The "shown / persisted exactly once" is literal — once the command exits, the hex is no longer in any process memory you control. Lose the keystore (and the passphrase), and the address is unrecoverable.

Flags:

- `--yes` / `-y` — skip the safety confirmation.

---

## `wallet remove NAME`

Remove a wallet from the pool. Cannot be undone.

```bash
$ skr-crypto wallet remove old-wallet
Remove wallet 'old-wallet'? This cannot be undone. [y/N]: y
Keystore passphrase: ********
✓ Removed wallet 'old-wallet'
ℹ Restart the service for the change to take effect.
```

For `KEY_PROVIDER=encrypted_file` this:
- Decrypts the keystore.
- Removes the named entry.
- Re-encrypts and atomically writes.

Returns the removed entry to the operator's terminal (the address only — the private key is never echoed). Useful if you want to confirm what was removed.

For other providers, the command warns:

```bash
$ skr-crypto wallet remove old-wallet
Remove wallet 'old-wallet'? This cannot be undone. [y/N]: y
⚠ KEY_PROVIDER=env is not directly editable. Update your secret store +
  remove 'old-wallet' from WALLETS in .env, then restart.
```

You apply the change manually.

Flags:

- `--yes` / `-y` — skip the destructive-action confirmation.

---

## `wallet rename OLD NEW`

Rename a wallet. **`encrypted_file` only** — for other providers, you'd need to update the env vars yourself (and the CLI tells you to).

```bash
$ skr-crypto wallet rename hot warm
Keystore passphrase: ********
✓ Renamed 'hot' → 'warm'
```

The wallet name is bound into the AES-GCM AAD, so a rename re-encrypts that entry with the new AAD. The salt and other entries are untouched.

The audit trail still shows past records under the old name — the rename only affects future records. There's no rewriting of history.

---

## `wallet encrypt`

One-shot initialisation of an encrypted keystore. Use cases:

- **Greenfield install** — no keys yet, just need a place to put them.
- **Migrating from another provider** — import an existing hex key into a fresh keystore.

```bash
# Empty keystore
$ skr-crypto wallet encrypt -o data/keystore.json
New keystore passphrase (will not echo): ********
Confirm passphrase: ********
✓ Created empty keystore data/keystore.json
ℹ Add wallets with `skr-crypto wallet add NAME` (or `wallet generate NAME` for a fresh key).

ℹ Next: edit your .env to use the new keystore:
    KEY_PROVIDER=encrypted_file
    KEYSTORE_FILE=/Users/.../skr-crypto/data/keystore.json
    # then either
    KEY_PASSPHRASE=...           # via secret manager / docker secret
    # or
    KEY_PASSPHRASE_FILE=...      # chmod 600 file
  Then `skr-crypto restart`.
```

```bash
# Migrate from KEY_PROVIDER=env (PRIVATE_KEY_HEX in your shell):
$ skr-crypto wallet encrypt --from-env PRIVATE_KEY_HEX --name main \
    -o data/keystore.json
New keystore passphrase: ********
Confirm passphrase: ********
✓ Created keystore data/keystore.json with wallet 'main' → TXxx...
```

```bash
# Migrate from a hex you have in hand (don't put it in shell history):
$ skr-crypto wallet encrypt --from-hex "$(pbpaste)" --name main -o data/keystore.json
```

Flags:

- `-o PATH` / `--output-path PATH` — keystore file location. Default: `data/keystore.json` under the install dir.
- `--from-hex HEX` — seed the keystore with this private key as wallet `--name` (default `default`). Mutually exclusive with `--from-env`.
- `--from-env VAR` — seed from a hex-valued env var. Mutually exclusive with `--from-hex`.
- `--name NAME` — wallet name to use when seeding. Default `default`. Ignored when neither seed source is provided.
- `--force` — overwrite an existing keystore at the destination. Destructive — old wallets are unrecoverable.

The passphrase is **prompted twice** (entry + confirmation) and never echoed. Keep the file `chmod 600` (the command sets it) and store the passphrase in your secret manager.

After this command, update `.env` and restart. The next `skr-crypto wallet generate ...` extends the same keystore.

---

## Failure modes

| Symptom | Cause | Resolution |
|---|---|---|
| `KEYSTORE_FILE is not set in .env` | Trying to use `wallet add/generate/remove/rename` with no keystore configured | Run `skr-crypto wallet encrypt` first |
| `keystore <path> already exists` | `wallet encrypt` without `--force` on an existing file | Verify you really want to wipe; pass `--force` |
| `passphrase rejected — keystore was not modified` | Wrong passphrase typed | Try again; if forgotten, the keys are unrecoverable |
| `invalid wallet name 'X'` | Used a non-ASCII / non-alphanumeric / dash / underscore character | Pick a name matching `[A-Za-z0-9_-]+` |
| `private key must be 64 hex chars` | Pasted with `0x` prefix or got a truncated value | Strip prefixes; ensure 64 hex chars |
| `Service is not reachable — falling back to offline listing` | Service is stopped during `wallet list` | Start the service or pass `--offline` explicitly |

---

## See also

- [Multi-wallet pool](../multi-wallet.md) — the model behind these commands
- [Encrypted keystore format](../keystore-format.md) — what the keystore file actually looks like
- [Key providers](../key-providers.md) — when to use which backend
- [Migrating to 1.4](../migration-1.4.md) — moving an existing single-wallet install to a multi-wallet keystore
