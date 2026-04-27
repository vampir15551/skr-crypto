# Security Model

## What this CLI is and isn't

`skr-crypto` is **operator infrastructure**, not a money path. The
service it manages does move USDT/TRX, but only via its authenticated
HTTP API — and the CLI deliberately does not proxy those endpoints.

The threat model below explains the boundary, what's defended, and
what the operator is responsible for.

## What the CLI can do

- Install / update / restart the service (operates on local files +
  process supervisor)
- Read state: balances, audit log, on-chain tx status, metrics
- Edit configuration (the operator's `.env`)
- Snapshot and restore the service's `data/` dir
- Run health diagnostics

## What the CLI cannot do

- Send TRX or USDT to anyone
- Sign a TRON transaction
- Read or extract the private key
- Bypass the service's audit trail or idempotency guarantees

If a future feature would let the CLI move funds, route it through the
service's HTTP API and audit it there. We won't accept PRs that add a
`skr-crypto send` (or equivalent).

## Threat model

### Adversaries we consider

1. **Operator with shell access** running `skr-crypto`. They can read
   `.env` (it's chmod 600, owned by them) and trigger anything on this
   list. We don't try to defend against the user we're shipping the
   tool to — the boundary is that they can't move money via the CLI,
   only via the API which has its own audit + auth.
2. **Network attacker** on the path between CLI and service. We only
   talk to the service over HTTP loopback by default. If the operator
   binds the service to 0.0.0.0, a reverse proxy with TLS is their
   responsibility.
3. **Compromised dependency** — a malicious bump of `requests` /
   `click` / etc. Mitigated by Dependabot for visibility and by
   `--require-hashes -r requirements.lock` on the service side.

### Adversaries we explicitly don't defend against

1. **Root on the host.** Owns everything regardless.
2. **Memory forensics.** AUTH_TOKEN lives in process memory while the
   command runs; we don't try to wipe it. Process is short-lived so
   the window is small.

## What the operator is responsible for

- **`.env` permissions.** Always chmod 600. `skr-crypto install`
  writes it that way; `doctor` flags any drift. Don't loosen them.
- **AUTH_TOKEN secrecy.** Treat the `.env` like a credential file —
  don't commit it, don't paste it in screenshots, don't email it.
- **Private key location.** `KEY_PROVIDER` selects the backend; see
  the service's own SECURITY docs for the trade-offs (`env` vs
  `file` vs `1password` vs `keychain`).
- **Update discipline.** Run `skr-crypto update` regularly so security
  fixes in upstream tronpy / requests / etc. land in your venv. We
  don't auto-update — that would be a worse default (silent breakage
  > known stale).

## Reporting a vulnerability

If you find a flaw that can compromise auth, leak the key, bypass the
audit, or otherwise cross the read-only boundary — please don't open
a public issue. Email the maintainer directly.

We aim to acknowledge within 72 hours and ship a patch within two
weeks for anything graded high.
