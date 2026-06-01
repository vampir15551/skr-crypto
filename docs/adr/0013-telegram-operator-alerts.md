# 0013 — Telegram operator alerts

## Status

Proposed. 2026-05-04. Targeted for 1.9.0.

## Context

After 1.7 (webhooks) and 1.8 (read-only UI), the operator-facing
surface still has a real gap: **there is no push channel for a
human**. Webhooks deliver to *systems*; the UI shows state on
demand. Today the operator either tails the audit log or finds
out about a sanctions reject / repeated `SEND_FAILED` / webhook
giveup because someone external told them.

In every prior real deploy the same workaround appears: external
log-shipping (Promtail / Vector / Datadog) → external alerting
(PagerDuty / Opsgenie / Slack-bot via Loki rules). That works for
shops that already run that stack; for the typical "one VPS, one
operator, USDT treasury" install it's a heavy lift.

We want a **first-party push channel that fits in the same
opt-in / minimal / restart-required envelope** as webhooks. The
channel of choice is Telegram — ubiquitous in the operator
demographic (former-USSR fintech, crypto-OTC, small back-offices),
free, no infrastructure to run, and Bot API is a single HTTP POST.

## Decision

Add an **opt-in** Telegram-alerts subsystem with the following
bounds, deliberately mirroring the webhook ADR (0011) so the
operator's mental model carries over:

1. **Audit log is still the source of truth.** Telegram
   delivery failure does NOT roll back, retry the on-chain
   transfer, or affect any other state. Same contract as webhooks.
2. **Opt-in via env.** `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
   together gate the feature. Either empty = disabled.
3. **Outbound only.** The bot calls `sendMessage` over Bot API
   via `requests`. We do NOT poll for incoming messages, do NOT
   register webhook with Telegram, do NOT support inline buttons /
   commands. The bot is a notifier, not a chatops surface.
4. **Direct HTTP, no python-telegram-bot.** The bot library
   pulls async / aiogram-style transitive deps; we hit
   `https://api.telegram.org/bot<TOKEN>/sendMessage` directly.
   ~30 lines, no new transitive deps.
5. **Receivers via chat_id.** The operator adds the bot to a
   group / DM, copies the numeric chat_id (Telegram returns it
   in any message), pastes it into `.env`. There is no
   `/start` handshake to track.
6. **Persistent delivery state in SQLite.** An `alert_deliveries`
   table records every attempt — the same shape as
   `webhook_deliveries`. Same `pending → delivered → failed →
   giving_up` state machine.
7. **Bounded retry policy.** Default backoff: 0s, 5s, 30s, 300s
   (4 attempts over ~6min). Telegram is far more reliable than
   arbitrary receiver URLs; long retries don't help and risk
   stale alerts arriving hours late.
8. **Trigger rules — strict event-driven.**

   - `ALERT_EVENTS` (csv): events that always alert. Default:
     `SEND_REJECTED,SEND_FAILED,WEBHOOK_GIVEUP`.
   - `ALERT_SEND_THRESHOLD_USDT` (Decimal): if set, also alert
     on `SEND_SUCCESS` whose amount >= threshold. Empty = off.
   - `ALERT_SANCTIONS_HIT_NOTIFY` (bool, default `true`):
     when on, `SEND_REJECTED` whose `details` contains
     `sanctions` is emphasised (separate message + 🚨 emoji).
9. **Quiet hours.** `ALERT_QUIET_HOURS_UTC=22-08` buffers
   non-critical alerts until the window closes; sanctions and
   `SEND_FAILED` are always-through. Default: empty (no quiet
   hours).
10. **Background worker thread**, parallel to the webhook
    worker. Delivery happens off the request path. /send
    returns 200 the moment audit fsync lands; the alert
    enqueue is a single SQLite INSERT.

Schema:

```sql
CREATE TABLE alert_deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_event_id  TEXT NOT NULL,
    event           TEXT NOT NULL,
    chat_id         TEXT NOT NULL,
    message         TEXT NOT NULL,        -- formatted human-readable text
    status          TEXT NOT NULL,        -- pending|delivered|failed|giving_up
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_response_at REAL,
    last_response_code INTEGER,
    last_error      TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX idx_al_status_next ON alert_deliveries(status, next_attempt_at);
```

Wire format (Telegram Bot API):

```http
POST https://api.telegram.org/bot<TOKEN>/sendMessage
Content-Type: application/json

{
  "chat_id": "-1001234567890",
  "text": "🚨 SEND_REJECTED — sanctions hit\n\nWallet: cold\nTo: T...XYZ\nAmount: 245.50 USDT\nReason: failed=sanctions",
  "parse_mode": "HTML",
  "disable_web_page_preview": true
}
```

Message formatter rules:

- Plain text + `<b>...</b>` HTML emphasis (Telegram's safest mode).
- Truncate `details` to 200 chars.
- Always include: event, wallet, to_address (last-6 obfuscated
  visually but full-width selectable), amount, idempotency_key,
  timestamp UTC.
- Sanctions hit: 🚨 prefix + "OFAC SANCTIONS REJECT" header.
- WEBHOOK_GIVEUP: ⚠️ prefix + delivery_id + URL.
- High-value SEND_SUCCESS: 💰 prefix + amount.
- Generic: ℹ️ prefix.

CLI surface (`cli/commands/alert.py`):

```text
skr-crypto alert setup           # walk the operator through bot/chat_id config
skr-crypto alert test            # POST a synthetic alert to verify wiring
skr-crypto alert list-deliveries # tabular view of recent attempts
skr-crypto alert retry <id>      # force-retry a giving_up delivery
```

`alert setup` is the friendliest piece: it walks through
"create the bot via @BotFather → DM the bot or add it to a
group → /start → forward any message to @userinfobot to grab
the chat_id" with copy-paste-ready prompts. Output is the .env
block to add.

API surface:

- `POST /api/v1/alert/test` — admin-scope only. Sends a
  synthetic alert to the configured chat_id. Used by the UI's
  "Send test" button.
- No new read endpoint for `alert_deliveries` in v1.9 — list
  via CLI is enough; the UI Notifications tab shows config
  (read from `/api/v1/health`-style config exposure) and the
  Send Test button.

UI surface (`/ui/#/notifications`):

- Visible to admin scope only.
- Shows current config (bot configured ✓ / not configured;
  chat_id; rules: events, threshold, quiet hours).
- "Send test alert" button → calls `/api/v1/alert/test`.
- No edit forms — all edits happen via `.env` + restart, same
  as webhooks. Consistent operator mental model.

Onboarding integration:

The interactive `skr-crypto install` wizard gains an optional
step (between "tunnel" and "gen key"):

```text
[6/9] Configure Telegram alerts? — push notifications for
      rejects / failures / sanctions hits (recommended)
       y) Yes, set up now
       N) No, skip (default)
```

If yes, three follow-up prompts: bot token (secret), chat_id,
optional `ALERT_SEND_THRESHOLD_USDT`. The wizard writes the
env keys directly. `_print_next_steps` adds:
"Test the bot: skr-crypto alert test".

`skr-crypto doctor` learns one new check: if
`TELEGRAM_BOT_TOKEN` is set, ping `getMe` and report
ok / unauthorized / network-down.

Metrics:

- `skr_crypto_alert_attempts_total{result="success|fail"}`
- `skr_crypto_alert_giveup_total`
- `skr_crypto_alert_queue_depth` — gauge

## Consequences

**Operators get push notifications without stitching together
log-shipping infrastructure.** Single-VPS deploys close the
biggest awareness gap (sanctions reject they only learn about
hours later) for the cost of 5 minutes of @BotFather setup.

**Same opt-in / restart-required envelope as webhooks.** No new
admin-edit surface, no UI form for the chat_id, no chance of a
compromised UI session re-pointing alerts to an attacker's
chat. ADR 0001's "money-adjacent settings change via .env +
restart" stands.

**Bounded blast radius.** A misbehaving Telegram (rate-limit,
network issue) only fills `alert_deliveries`. No back-pressure
onto /send. The 4-attempt schedule means a permanently-down
bot stops generating retries within ~6 minutes.

**No new daemons.** Reuses the existing
"audit hook → enqueue → worker thread" pattern from webhooks.
The alert worker is a second daemon thread, structurally
identical. Receipt-timeout / balance-low alerts (which would
require a periodic sweeper) are explicitly **out of scope for
1.9** and parked for 1.10.

**Cost: one daemon thread + 0–5 outbound HTTPS/sec depending
on event load.** For a typical operator (a few SEND_REJECTs a
day, an occasional WEBHOOK_GIVEUP), background load is
effectively zero.

**Wire-format additions on /api/v1:** one new endpoint
(`POST /alert/test`, admin scope). No changes to existing
endpoints. No new audit event types.

**Bot token in .env.** Same risk profile as `AUTH_TOKEN` and
`WEBHOOK_SIGNING_SECRET` — 0600 file, never logged, scrubbed
from `skr-crypto config show` output.

## Alternatives considered

- **`python-telegram-bot` library.** Pulls in `httpx`, async
  glue, optional job queue, parsers we won't use. Adds
  install-time weight for one HTTP call. Rejected.
- **Slack bot in addition.** Discussed and dropped at the
  product-scoping stage — Slack adoption in the target
  operator demographic is far smaller than Telegram, and
  doubling the transport doubles the surface (two formatters,
  two retry queues, two test paths). If demand materialises,
  same pattern will copy cleanly to a `slack.py` sibling in a
  later minor.
- **Bidirectional bot (commands like `/balance`).** Tempting
  but turns the bot into a money-adjacent control surface.
  ADR 0001 / 0012 say money flows through the API, full stop.
  Outbound-only stays consistent.
- **Per-chat-id subscriptions table editable via UI.** YAGNI
  for v1.9. The single-chat-id model handles "operator's DM
  group" and "team alerts channel" — the two patterns we see.
  Multi-target with rules per target is a v2 problem.
- **Reuse `WEBHOOK_URLS` with a `tg://` scheme.** Looked
  briefly. Forces the webhook delivery code to grow a transport
  abstraction (HTTPS POST vs Telegram Bot API quirks) for one
  use case. Cleaner to keep webhooks 1:1 with HTTP and add a
  parallel module.
- **Severity-based fan-out (critical → SMS, normal → TG).** Out
  of scope. SMS adds a paid provider dependency; the universe
  of "critical" events that warrant SMS is small enough to
  serve by a separate Telegram chat the operator monitors more
  closely.
- **Periodic checks (balance low, receipt timeout).** Out of
  scope for v1.9 on principle: those need a new sweeper
  daemon. Parked for v1.10 where they ship together with the
  balance-monitor daemon.

## Related

- [ADR 0001](0001-sync-only-architecture.md) — alert delivery
  is on a daemon thread, never on the request path. Money path
  stays sync.
- [ADR 0004](0004-audit-hard-error.md) — audit is the source
  of truth; alerts are derived. Audit fsync failure still
  5xx's the caller; alert delivery failure does NOT.
- [ADR 0011](0011-opt-in-webhooks.md) — alerts mirror webhook
  semantics deliberately. The two systems are independent;
  enabling one does not enable the other.
- [ADR 0012](0012-read-only-web-ui.md) — the UI's
  Notifications tab shows config + test button. No config
  edits in the UI; same envelope as webhooks.
- `tests/test_alerts.py` — invariant tests including
  "delivery failure does NOT block /send", "sanctions hit
  bypasses quiet hours", "bot token is never logged",
  "synthetic test message is clearly marked".
