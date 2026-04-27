# Configuration commands

`config` is a click subgroup, so the actual commands are
`config show`, `config edit`, `config validate`.

## `config show`

```bash
skr-crypto config show [--unsafe-show-secrets]
```

Prints `.env` as a table. Sensitive keys (`AUTH_TOKEN`,
`TRONGRID_API_KEY`, `PRIVATE_KEY_HEX`) are masked to the first 4
characters by default. `--unsafe-show-secrets` opts back into showing
plaintext — use only when you know nobody's looking at your screen.

## `config edit`

```bash
skr-crypto config edit
```

Opens `.env` in `$EDITOR` (or `$VISUAL`, falling back to `nano` /
`vim` / `vi`). Reminds you to restart the service after saving — the
service reads env vars only at startup.

## `config validate`

```bash
skr-crypto config validate
```

Runs the service's own `validate_config()` against the current `.env`
without starting the service. Catches the common foot-guns before
they're an outage:

- `AUTH_TOKEN` unset
- `MIN_TRX_RESERVE` lower than the per-tx fee_limit
- `KEY_PROVIDER` typoed
- Bad `USDT_CONTRACT` format
- etc.

Exits 0 on success, non-zero with the failing checks on stderr.
