# SKR Crypto CLI

Operator CLI for the SKR Crypto microservice fleet. Installs, updates,
and observes the underlying payout services. The CLI is deliberately
**read-only on the money path** — money moves only through the
service's authenticated HTTP API.

## What it gives you

- One-command install, update, and lifecycle management
- Read-only inspection: balances, on-chain tx status, audit log
- Diagnostics that flag misconfiguration before it bites
- Stable exit codes per error class for CI / wrapper scripts
- A documented, versioned, tested tool — not a one-off shell script

## Quick start

```bash
pip install skr-crypto
skr-crypto install                  # → ~/.skr-crypto, generates AUTH_TOKEN
skr-crypto config edit              # → set TRONGRID_API_KEY etc.
export PRIVATE_KEY_HEX='<your hex>'
skr-crypto start
skr-crypto status                   # ✓ running, healthy, version printed
```

`skr-crypto help` lists every command, grouped.

## Where to next

- [Install](install.md) — three install paths in detail
- [Commands](commands/index.md) — full reference
- [Configuration](configuration.md) — every env var, what it means, defaults
- [Security](security.md) — threat model and operator responsibilities
- [Releases](releases.md) — how to cut a tag and what CI does
- [Changelog](changelog.md) — what changed when
