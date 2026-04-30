# `skr-crypto token`

Manage per-caller API tokens. Added in 1.5.0 — see [ADR 0008](../adr/0008-per-caller-api-tokens.md) for the design rationale.

```text
skr-crypto token
├── list [--include-revoked] [--json]
├── show ID [--json]
├── create --name N --scope S[,S...]
├── revoke ID [--yes]
└── rotate ID [--yes]
```

Tokens live in the same SQLite DB as the idempotency store
(`IDEMPOTENCY_DB_PATH`). The CLI talks to that DB directly — no
running service required for `list`/`revoke`/`rotate`. Service picks
up new tokens on next restart (or, for `revoke`, on the next
authenticated request — revocation is checked at verification time).

---

## Scopes

Four well-known values:

| Scope | Endpoints |
|---|---|
| `admin` | every endpoint, including future write-ops |
| `send` | `POST /send` + everything `read` covers |
| `read` | `GET /balance`, `/wallets`, `/risk`, `/health` |
| `metrics` | `GET /metrics` only |

Implication graph:

- `admin` ⊇ `send` ⊇ `read`
- `admin` ⊇ `metrics`
- `read` and `metrics` are **siblings** (a metrics scraper shouldn't see balances).

Pick the narrowest scope that satisfies your caller's needs:

| Caller | Scopes |
|---|---|
| Production back-office that initiates payouts | `send,read` |
| Internal dashboard that polls balances + risk | `read` |
| Prometheus scraper | `metrics` |
| You, the operator | `admin` |

---

## `token list`

Default lists every active (non-revoked) token. `--include-revoked`
adds the historical entries.

```bash
$ skr-crypto token list
┏━━━━━━━━━━━━ API tokens ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ ID         Name              Scopes      Created            Last used     Status ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 8d7e6b...  backoffice-prod   send,read   2026-04-30T13:00   2026-04-30T15:21  active │
│ 4f3a1c...  metrics-prom      metrics     2026-04-30T13:01   2026-04-30T15:30  active │
│ a2b9d4...  dashboard-internal read       2026-04-30T13:05   2026-04-30T14:42  active │
└────────────────────────────────────────────────────────────────────────────────┘
```

`--json` for scripts:

```bash
$ skr-crypto token list --json
[
  {
    "id": "8d7e6b...",
    "name": "backoffice-prod",
    "scopes": ["read", "send"],
    "created_at": "2026-04-30T13:00:00+00:00",
    "last_used_at": "2026-04-30T15:21:33+00:00",
    "revoked_at": null,
    "created_by": "operator"
  },
  ...
]
```

---

## `token show ID`

Single-token view with all metadata. Plaintext is **never** shown
(it was returned only at `create` time).

```bash
$ skr-crypto token show 8d7e6b
┏━━━━━━━━━━━━━ Token 8d7e6b ━━━━━━━━━━━━━━━┓
┃ Field          Value                     ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ id             8d7e6b...                 │
│ name           backoffice-prod           │
│ scopes         read,send                 │
│ created_at     2026-04-30T13:00:00+00:00 │
│ last_used_at   2026-04-30T15:21:33+00:00 │
│ revoked_at     None                      │
│ created_by     operator                  │
└──────────────────────────────────────────┘
```

---

## `token create`

Mints a new token. The plaintext value is shown **once** and cannot be
recovered — copy it the moment you see it.

```bash
$ skr-crypto token create --name backoffice-prod --scope send,read
✓ Created token id=8d7e6b1234ab name='backoffice-prod'
ℹ Scopes: read,send

⚠ The token is shown ONCE — copy it now.
skr_F-z9T3qG4yPm8aEW1Lc2hKrXuJsvYnoMt6BjbN0iVCa

ℹ Restart the service to load the token. Use it via X-API-Key: <token> header.
```

The token immediately becomes valid in the persistent store. The
service caches active tokens in memory after boot, but verification
is per-request — a new token is picked up on the next authenticated
call (no restart required, despite the message above which is a
gentle hint for ops hygiene).

Flags:

- `--name N` — required. Operator-chosen label. Used in logs and audit.
- `--scope S[,S...]` — comma-separated scopes. Default `send,read`.

---

## `token revoke ID`

Marks a token as revoked. The next request using that token fails
with 401 `Token has been revoked`. In-flight requests complete (we
check at the start of the request, not mid-handler).

```bash
$ skr-crypto token revoke 8d7e6b
Revoke token 8d7e6b (name='backoffice-prod')? Cannot be undone. [y/N]: y
✓ Revoked token 8d7e6b
```

Idempotent — revoking an already-revoked token is a no-op.

Flags:

- `--yes` / `-y` — skip the confirmation prompt.

---

## `token rotate ID`

Atomic create-new + revoke-old. Use when a token is suspected leaked
but the service mustn't go down. The two tokens coexist long enough
for callers to swap their config:

```bash
$ skr-crypto token rotate 8d7e6b
Rotate token 8d7e6b (name='backoffice-prod')? A new token replaces
it; the old one is revoked. [y/N]: y
✓ Rotated: revoked id=8d7e6b, created id=c3a4d5
⚠ The new token is shown ONCE — copy it now.
skr_xK3pQ9aEoB1zT5fNgRwvMsHbY7uJL2dCiVy0n4mUjAr
```

The new token has the **same name** and **same scopes** as the old
one. `created_by` on the new record points at the old token's id, so
forensic queries can trace rotation chains.

---

## Recommended workflow

### First-time setup (replacing legacy `AUTH_TOKEN`)

```bash
# 1. Mint scoped tokens for each caller class.
skr-crypto token create --name backoffice-prod --scope send,read
skr-crypto token create --name dashboard --scope read
skr-crypto token create --name metrics-prom --scope metrics

# 2. Distribute each plaintext to its respective consumer.
#    (Configure secret manager, redeploy callers, etc.)

# 3. Verify everything is using the new tokens.
skr-crypto token list   # last_used_at should be recent for all three

# 4. Drop legacy AUTH_TOKEN from .env + restart.
#    The deprecation warning stops appearing in service logs.
```

### Suspected leak

```bash
skr-crypto token rotate <leaked-id>
# → distribute the new plaintext to the legitimate caller
# → confirm legitimate traffic works
# → leaked token already 401's (revoked atomically with rotation)
```

---

## Audit attribution

Every authenticated request now records a `token_id` in the audit
record alongside `client_ip`:

```json
{
  "event": "SEND_SUCCESS",
  "wallet": "cold",
  "from_address": "TColdxxx...",
  "to_address": "TRX9SbJ...",
  "amount": "245.50",
  "txid": "abc123",
  "idempotency_key": "invoice-741",
  "client_ip": "10.0.0.42",
  "token_id": "8d7e6b1234ab",
  "result": "broadcast",
  "details": "elapsed=1.43s risk=low"
}
```

Forensic queries that previously had to guess from IPs can now
answer "which back-office service initiated this?" deterministically:

```bash
grep '"token_id":"8d7e6b1234ab"' data/audit.log | wc -l
# count of every action `backoffice-prod` ever triggered
```

The legacy `AUTH_TOKEN` path records `token_id=legacy`.

---

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `403 INSUFFICIENT_SCOPE` | Token valid but lacks the required scope for this endpoint | Use a token with the right scope, or rotate this token to add scopes |
| `401 Token has been revoked` | Token was `revoke`d after the caller cached it | Mint a new token + redeploy caller |
| `401 Invalid or missing X-API-Key` | Typo, expired wheel, plaintext lost | Re-issue with `token create` |
| `[AUTH] Legacy AUTH_TOKEN in use` (boot warning) | `AUTH_TOKEN` still in `.env` | Migrate callers to scoped tokens; remove `AUTH_TOKEN` |
| `token commands require cryptography` (CLI error) | Bare-CLI install without `[server]` extras | `pip install 'skr-crypto[server]'` |

---

## See also

- [HTTP API: auth section](../api-reference.md#authentication) — wire format
- [Security model: caller with token](../security.md#2-caller-code-with-the-api-token) — threat model
- [ADR 0008](../adr/0008-per-caller-api-tokens.md) — design rationale + alternatives considered
