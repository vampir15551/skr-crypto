# ADR 0001: Sync-only architecture, no asyncio on the money path

## Status

Accepted. Locked in for the lifetime of `skr-crypto` 1.x. A change here
would be a major-version event with a deprecation cycle.

## Context

`skr-crypto-server` is a FastAPI service whose primary job is to sign
and broadcast USDT TRC-20 payouts. Every request that reaches
`POST /api/v1/send` touches three pieces of state that have to stay
consistent with each other:

1. The idempotency state machine (SQLite, WAL mode) — reserves a slot,
   commits a stored response, releases on pre-broadcast failure.
2. The audit log (append-only JSONL with explicit `fsync`) — the
   system of record for every state transition that affects on-chain
   reality.
3. The TRON RPC client — the network call that actually moves money.

The audit row must hit disk before the API responds 200. The
idempotency commit must happen after the audit fsync. The broadcast
must not be retried automatically. These ordering constraints are the
heart of the service's correctness.

FastAPI is async by default. Route handlers can be defined as `async
def` and the framework will integrate them with its event loop;
synchronous handlers are run in starlette's thread pool. This ADR
records the choice between making the internal state machines async
versus keeping them synchronous and using async only at the framework
boundary.

## Decision

**All internal logic is synchronous.** `IdempotencyStore`,
`AuditStore`, `TronClient`, `KeyProvider`, and every helper they call
are `def`, not `async def`. They use `requests` (not `httpx.AsyncClient`),
`sqlite3` from the stdlib (not `aiosqlite`), and `os.write` /
`os.fsync` directly.

**Async exists only at the framework boundary.** Route handlers in
`skr_crypto/server/routes.py` may be `async def` (and are, where it
matters for streaming or when starlette's typing demands it), but
nothing past the route handler awaits anything. A handler reads its
inputs, calls into sync code, and returns the response.

This is enforced by code review and by the absence of any async
runtime in the dependency tree past FastAPI itself.

## Consequences

**Throughput is bounded by RPC latency to TronGrid.** Typical TronGrid
calls are 50-2000ms. With a single uvicorn worker and starlette's
default thread pool, the steady-state limit is roughly
`pool_size / mean_rpc_latency` requests per second. For a single-operator
treasury that's fine; for a payments fleet it would not be. We are not
a payments fleet.

**Single-process design.** There is one uvicorn worker. There is one
SQLite WAL connection. There is one open file descriptor for the audit
log. There is one `TronClient` singleton holding the private key.
Multiple workers would require coordinating across processes (a real
queue, a real lock manager) and the architecture is not currently
shaped for that.

**Connection pools are not shared across requests in interesting
ways.** The single SQLite WAL connection is shared via thread-local
locking; the single TRON RPC client reuses HTTP keep-alive across
threads. This is acceptable because we have one of each. If we ever
needed N TRON RPC connections, we'd need a real pool — but we don't.

**The audit-and-commit critical section is two function calls in the
same call stack.** No `await` between them, no chance of a cancellation
arriving in the middle, no event-loop scheduling decisions to reason
about. This is the single most important property of the architecture
and the primary reason for the choice.

**Postmortems are easier.** Sync threading bugs are well-understood.
Async race conditions in production code that touches money are not
something any of us want to debug at 3 AM.

## Alternatives considered

**Pure async (FastAPI + httpx + aiosqlite + aiofiles).** Rejected.
The audit-then-commit ordering becomes a cooperatively-scheduled
critical section across `await` points, with implicit cancellation
behaviour at every `await` boundary. We don't need the throughput, and
debugging an event-loop ordering bug in a payment service is a class
of problem we explicitly do not want to take on. The cost of being
boring here is throughput we don't need; the cost of being clever is a
duplicate-payment bug we cannot recover from.

**Threads-only with Flask.** Rejected. FastAPI gives us
pydantic-validated request/response models, automatic OpenAPI, and a
clean dependency injection story. Flask would require us to
hand-roll request validation and serialisation, which is more code we
don't want to maintain. The async surface is the price of admission;
we pay it at the edge and stop there.

**Mixed sync/async with explicit `run_in_executor` boundaries.**
Rejected. This is the worst of both worlds: the explicit complexity of
async at every layer plus the implicit complexity of "is this function
called from sync or async context?". If the answer to that question is
ever "it depends", the code is unsafe.

## Related

- [`GUIDELINES.md` § Sync everywhere on the money path](../../GUIDELINES.md)
- [`ARCHITECTURE.md` § Process model](../../ARCHITECTURE.md#process-model)
