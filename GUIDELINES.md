# Guidelines

> The bar for accepting code into `skr-crypto`. Read this before you open a PR.

These are not preferences. They are the principles the design rests on, and a PR that violates one needs an extraordinarily good reason — not a stylistic argument.

---

## Sync everywhere on the money path

No `asyncio` inside `/api/v1/send`, the audit writer, the idempotency store, the `TronClient`, or anything they call. Async is allowed at the HTTP framework boundary because FastAPI gives it to us for free; nothing past the route handler may `await`. The reason is determinism: the audit-and-commit sequence is a two-statement critical section in the same call stack with no cooperative scheduling between them, and we want it to stay that way. Event-loop surprises in a money path are post-mortem fuel we don't need. Throughput-bound? Add a worker, add a queue — don't go async.

---

## Audit-first

Every state transition that affects on-chain reality writes a fsync'd audit row **before** the API returns. That includes successful sends, rejected sends, broadcast failures, key load events, and reconciliation outcomes. The audit row is the system of record; the SQLite store is a derived view. If the audit write raises, the request fails with `500` and the idempotency slot stays `PENDING` for reconciliation. There is no silent degradation: no in-memory buffer, no "best-effort" retry, no fallback file. If the audit log can't be written, the service isn't doing its job and should refuse to.

---

## All RPC calls have explicit timeouts

Every `requests` call, every `tronpy` HTTP call, every outbound network operation has a `timeout=` argument. Default is 15 seconds against TronGrid. Idempotent reads (account lookups, contract queries) are wrapped in up to 3 retries with exponential backoff and jitter. **Writes are not retried** — `broadcasttransaction` is the canonical example; we'd rather end up with an `UNKNOWN` slot and a human-in-the-loop reconciliation than a duplicate payment from a blind retry. A PR that introduces `requests.get(url)` without `timeout=` will fail review on the first read.

---

## Idempotency is a hard contract

Same `Idempotency-Key`, same outcome — exactly. A `COMMITTED` slot replays the stored response byte-for-byte. A `RELEASED` slot is treated as a fresh slot. An `UNRESOLVED` slot returns `409` and refuses to retry, ever, automatically. Auto-retrying an `UNKNOWN` is the duplicate-payment bug we are most paranoid about, and the protection lives in the state machine. If you find yourself adding a code path that re-uses an `UNRESOLVED` key without human action, stop — you're about to ship the bug. The CLI surfaces the manual reconciliation playbook (`skr-crypto audit --unresolved`, `skr-crypto reconcile --resolve <key>`) precisely so this never has to be automated.

---

## Logs are forensic, not chatty

Every payout emits 3 to 5 structured log lines describing the decisions made — chosen `fee_limit`, energy estimate, balance snapshot, broadcast response, audit sequence number. Each line is structured (key=value pairs or JSON), single-line, and stable across versions. There are **no DEBUG-only paths** in production behaviour; if a line is useful at DEBUG level, it's useful in production. The corollary: don't add log lines for control flow ("entering function X"), don't add log lines for happy-path successes ("got 200 from health check"), and don't paper over a missing branch with a `log.warning` and a `pass`.

---

## No money path in the CLI

The CLI manages, observes, audits — it never broadcasts. There is no `skr-crypto send`, there will never be a `skr-crypto send`, and a PR that adds one will be closed without review. The reason is structural: the CLI runs in the operator's terminal, with the operator's environment, with whatever shell history and tmux scrollback that implies. The service is the only place that loads the private key, and it loads it via a `KeyProvider` with audited boot sequence. Putting a broadcast path in the CLI means duplicating that machinery in a context that wasn't designed for it. The cost of saying no here is one extra HTTP request from a script that wants to send funds. That's a fine cost.

---

## Tests cover the boundary, not the framework

Mock TronGrid (the HTTP layer) and the `TronClient` singleton. Never make real RPC calls in tests; never let CI hit a real testnet. We don't test that FastAPI routes requests correctly, we don't test that SQLite commits transactions, we don't test that `tronpy.PrivateKey` signs deterministically. We test the seams: the audit-then-commit ordering, the state-machine transitions on every failure mode, the `KeyProvider` refuses to boot when permissions are wrong, the `X-API-Key` middleware uses constant-time compare. A PR that adds a test calling out to a live TronGrid endpoint will be rejected; the resulting flake budget is unaffordable and the test isn't testing what it thinks it's testing.

---

## Versioning: SemVer, pre-1.0 carve-out

We follow [Semantic Versioning](https://semver.org/). Pre-1.0 reserved the right to break things in a minor version with a clear note in [`CHANGELOG.md`](CHANGELOG.md); now that we're at `1.0.0`, breaking changes go in major versions only and require a deprecation cycle of at least one minor. The release process — tag, workflow, signed wheel, GitHub Release — is documented in [`RELEASE.md`](RELEASE.md). Every release ships with a wheel attached to its GitHub Release; there is no PyPI publish step and there is not going to be one.

---

If you've read this far, the next file to read is [`ARCHITECTURE.md`](ARCHITECTURE.md). The principles above only make sense once you've seen the shape of the system they're protecting.
