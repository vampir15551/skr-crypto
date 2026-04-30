# 0012 — Read-only operator web UI

## Status

Accepted. 2026-04-30. Introduced in 1.8.0.

## Context

Through 1.7 the only operator-facing surfaces are CLI (read-write,
local) and HTTP API (read-write, network). Operators of the real
deploys have surfaced one consistent ask:

- **Compliance / finance / engineers without shell access** want a
  way to look at recent transfers, balances, and risk reports.
  Right now they ping the operator and ask for `skr-crypto audit`
  output, which becomes a manual relay.
- **Even the operator themselves** prefer a tabular view over `tail
  -f data/audit.log | jq .` for incident triage.

The design constraint: **money-moving must NOT be in the UI**. ADR
0001 says payouts go through the authenticated HTTP API, and we
reaffirm that — the UI is a strict read-only client of `/api/v1/`.
There is no "send" button. There never will be one.

## Decision

Ship a **read-only static SPA** mounted at `/ui/`, served as static
files by FastAPI from `skr_crypto/server/ui/`. The SPA is plain
HTML + Alpine.js (3KB minified, zero build step) + minimal CSS.

Pages (single-file SPA with hash-based routing):

```text
/ui/                     login screen (paste API token)
/ui/#/dashboard          health summary + node connection
/ui/#/wallets            wallet list with live balances + auto-pick winner
/ui/#/audit              audit log table, paginated, filterable
/ui/#/risk               risk-lookup form
/ui/#/tokens             token list, create/revoke (admin scope only)
/ui/#/webhooks           recent delivery list (admin scope only)
```

Auth model:

1. User opens `/ui/`. The page's JS prompts for an API token
   (via a paste field — never echoed to the screen).
2. Token is stored in `sessionStorage` only. Refreshed page = re-prompt.
   No persistent cookie.
3. Every API call from the UI sends `X-API-Key: <token>` —
   the same auth as the existing CLI / external callers.
4. The token's scope determines which tabs render. A `read` token
   sees Dashboard / Wallets / Audit / Risk; an `admin` token also
   sees Tokens / Webhooks; a `metrics` token sees nothing useful.

New API endpoints to support the UI (both `read` scope):

- `GET /api/v1/audit?since=<id>&limit=<n>` — paginated audit reader.
  Reads from `AUDIT_LOG_FILE` (or stdout buffer in dev). Returns
  records sorted by id descending; the UI fetches batches.
- `GET /api/v1/wallets/all` — alias for `/api/v1/wallets` (the UI's
  page-load fetch). No semantic difference; pre-existing.

Static files served under `/ui/` are bundled in the wheel. The CSS
is hand-written (~200 lines, `prefers-color-scheme` aware). The JS
is `<300 lines` of vanilla + Alpine.js loaded from local copy
(zero CDN dependency for offline / air-gapped deploys).

Mount logic:

```python
# server.py
from fastapi.staticfiles import StaticFiles

# UI is served from skr_crypto/server/ui/static/
ui_dir = Path(__file__).parent / "ui" / "static"
if ui_dir.exists():
    app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")
```

Auth gate is on the API side, not the static-file side. Anyone can
read the HTML; without a valid token, every API call from the UI
returns 401, so the visible state stays empty / a login prompt.

CSP hardening on the `/ui/` response: strict, no inline JS except
for what we ship; no remote loads. The only things the page can
fetch are its own `/api/v1/*` and its own `/ui/static/*`.

## Consequences

**Compliance / finance / engineers can see service state without
shell access.** Reduces the operator-as-relay friction.

**No money-path code in the UI.** The HTML has no `/send` form. The
JS has no `POST /api/v1/send`. Searching the bundle for `/send`
returns zero hits. We add a CI check that asserts this.

**Token reuse.** UI uses the same `X-API-Key` mechanism as every
other client. No new auth surface, no session cookies, no CSRF
machinery (because there are no money-moving forms).

**Tiny footprint.** ~10KB total static bundle (HTML + CSS + Alpine.js
copy). The wheel size grows by single-digit KB.

**No build step.** The UI is hand-written, not transpiled. Anyone
can read the source. Updates ship through the same release pipeline
as everything else.

**Versioning.** Wire-format goldens (ADR 0007) cover the new
`/api/v1/audit` endpoint. The UI itself doesn't have a wire format
to version — it's an HTML page that calls the same versioned API.

## Alternatives considered

- **SvelteKit / React / Vue with a build pipeline.** Industry-standard
  for any non-trivial dashboard. Rejected because:
  1. Adds a Node.js build step to the release pipeline (was Python-only).
  2. Tens of MB of `node_modules` per build.
  3. Operators reading the source need to understand a separate
     ecosystem.
  4. The use case is genuinely tiny (a few tables, a few forms);
     250 lines of vanilla + Alpine.js does the job.
- **htmx instead of Alpine.js.** Equally tiny. Slightly less
  declarative for the simple state we have. Tie-breaker: Alpine has
  better `x-data` for the login flow.
- **Server-rendered HTML (Jinja2 templates).** Would mean the UI
  lives on the request path, not as a static SPA. Pros: no JS at
  all. Cons: every page render goes through the FastAPI process,
  which is currently strictly money-handling. Keeping the UI as
  static + JS-driven isolates it cleanly.
- **Embed a `/send` form behind a "danger" button.** Tempting for
  ops convenience. Rejected — money path goes through the API
  only, full stop. ADR 0001 is the contract.
- **Cookie-based session auth instead of paste-the-token.** A
  cookie's main advantage (auto-fill on revisit) is also its main
  risk (XSS-stealable, CSRF-vulnerable for money-moving). Since
  the UI is read-only, the marginal advantage is small; the
  prompt-per-tab UX is a fair trade for less attack surface.
- **Use OpenAPI's auto-generated `/docs`.** We disabled `/docs` in
  production (`docs_url=None`) for security reasons (don't expose
  API surface). A custom UI gives us read-only intent + a tabular
  view that auto-docs can't match.

## Related

- [ADR 0001](0001-sync-only-architecture.md) — money path stays
  through the API; UI is read-only.
- [ADR 0008](0008-per-caller-api-tokens.md) — UI uses the
  same scope-aware auth as every other client.
- [ADR 0011](0011-opt-in-webhooks.md) — webhook delivery state is
  visible in the UI's webhook tab.
- `tests/test_ui.py` — invariant tests including "no /send in
  bundle" + "no inline event handlers" + "API calls land within
  documented endpoints."
