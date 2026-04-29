# Operations

> Operating `skr-crypto` in production. This file is the index. Incident playbooks live as separate runbooks under [`runbooks/`](runbooks/) — each is a self-contained "I have this incident, what do I type" doc.

When something breaks, jump straight to the runbook that matches
the symptom — they are written to be read in 30 seconds, under
stress.

---

## Service model

- **Single-instance, single-worker.** One uvicorn worker, one
  `TronClient` singleton, one open audit fd, one SQLite WAL
  connection. Do not scale horizontally — see
  [ADR 0001](docs/adr/0001-sync-only-architecture.md).
- **Synchronous on the money path.** No `asyncio` past the FastAPI
  route handler. Audit-then-commit is two function calls in the
  same call stack with no `await` between them.
- **Durable state at `$SKR_CRYPTO_HOME/data/`.** `audit.log`
  (append-only JSONL, fsync per record) and `idempotency.db`
  (SQLite WAL). Both must be on a real disk, not tmpfs. Backups,
  reconciliation, and rotation all key off these two files.

---

## Day-zero install

See the [README quickstart](README.md). The README is canonical;
don't duplicate it here. In one paragraph: install the wheel from
the GitHub Release, set `AUTH_TOKEN`, `TRONGRID_API_KEY`, and the
chosen `KEY_PROVIDER` target in `.env`, then `skr-crypto start`.
`skr-crypto doctor` flags any drift before the first request lands.

---

## Incident playbooks

Each runbook starts with a one-line **Symptom / Cause / TL;DR fix**
triplet so you can confirm you're in the right doc within seconds.

1. **[`runbooks/stuck-broadcast.md`](runbooks/stuck-broadcast.md)** —
   client called `/send` and is hanging. Find the in-flight
   request, confirm on tronscan, rely on idempotent retry.
2. **[`runbooks/rpc-outage.md`](runbooks/rpc-outage.md)** —
   TronGrid is down or rate-limiting. Confirm it's TronGrid not
   us, then rotate the API key or fail over to a backup endpoint.
3. **[`runbooks/key-rotation.md`](runbooks/key-rotation.md)** —
   rotating the TRON treasury private key (suspected leak or
   routine). Generate, fund, **drain manually via TronLink** (this
   CLI cannot move money), update provider, restart, verify.
4. **[`runbooks/audit-recovery.md`](runbooks/audit-recovery.md)** —
   `audit.log` is corrupt or unreadable. Stop, snapshot, trim the
   bad tail, restart, reconcile. SECURITY-relevant: log the
   recovery in an out-of-band channel.
5. **[`runbooks/unresolved-key.md`](runbooks/unresolved-key.md)** —
   client got `409 IDEMPOTENCY_UNRESOLVED`. Verify on tronscan,
   then either mark `committed` with the found txid or delete the
   slot row. **Never blindly retry.**

---

## Logs

```bash
journalctl -u skr-crypto -f                    # systemd
docker compose logs -f skr-crypto              # docker compose
skr-crypto logs -f                             # CLI tail (works against either)
```

Log lines are structured `key=value` pairs, single-line per event,
stable across versions. Filter for `[SEND]`, `[BOOT]`,
`[STARTUP_CHECK]`, `[BALANCE]`, `[HEALTH]` to scope to a phase. See
[`GUIDELINES.md`](GUIDELINES.md) § "Logs are forensic, not chatty".

---

## Backups

```bash
skr-crypto backup                              # snapshot data/ → data/backups/, rotated
skr-crypto restore data/backups/<timestamp>.tar.gz  # stops service first; refuses if up
```

`tar -czf` of the entire `data/` directory — audit log,
idempotency DB, reconciliation artefacts. Default rotation keeps
the last 14. Ship them off-host as part of your normal backup
pipeline; the CLI does not push to remote storage on its own.

---

## Metrics

Prometheus scrape endpoint at `GET /api/v1/metrics` (X-API-Key
required). The Grafana dashboard ships in the repo at
`deploy/grafana/skr-crypto.json` — import it and point at the
Prometheus that scrapes the service. Key panels: broadcast result
rate, unresolved-key counter, energy-burn distribution, RPC
latency histograms.

The scrape does no RPC — gauges are refreshed on `/balance`,
`/health`, and post-`/send` paths. Scrape interval doesn't have to
be tight; 30s is comfortable.

---

## Auto-shutdown

The service shuts itself down after `SHUTDOWN_TIMEOUT` seconds
(default 600s) of no inbound HTTP requests. Keys live in process
memory; an idle service is a service with a key sitting around for
no reason, so we exit and force a deliberate restart for the next
batch. Every request resets the timer. The shutdown closes the
audit fd, the SQLite connection, and (under
`KEY_PROVIDER=1password`) locks the `op` session before exiting.

**When to disable.** Set `SHUTDOWN_TIMEOUT=0` if the service is
behind a load balancer with health-check frequency longer than the
timeout, or if you're running an integration suite with gaps
between calls. For normal operator use, leave it on. Mid-broadcast
exits are gated against in-flight requests; in the rare event one
slips through, the next boot's reconciliation surfaces the orphan
via the unresolved-key playbook.

---

## Upgrades

See [`RELEASING.md`](RELEASING.md) for the maintainer side and the
README for the operator-facing flow. The short version:
`skr-crypto update` pulls the latest GitHub Release wheel,
verifies the SHA256, installs it, restarts. There is no
auto-update; updates are deliberate.

---

## Where to read next

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — system design, state
  stores, idempotency state machine.
- [`SECURITY.md`](SECURITY.md) — threat model, operator
  responsibilities, post-leak rotation.
- [`GUIDELINES.md`](GUIDELINES.md) — engineering principles.
- [`docs/adr/`](docs/adr/) — architecture decision records.
