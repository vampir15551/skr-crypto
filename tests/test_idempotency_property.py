"""Property-based tests for the idempotency state machine.

Hypothesis fuzzes sequences of (reserve, commit, release) calls
across multiple keys and threads. The properties we assert are
**invariants** — not specific examples we've thought of, but
universal truths the store must guarantee.

If Hypothesis ever finds a counter-example, the failing input is
serialised in `.hypothesis/examples/` so the regression triggers
deterministically on the next CI run.
"""
from __future__ import annotations

import threading
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from skr_crypto.server.idempotency import IdempotencyStore

pytestmark = [pytest.mark.property]


# Idempotency keys: alphanumeric, length 1-32. Avoids edge cases
# Pydantic would already reject (too long, non-printable).
_keys = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1, max_size=8,
)
_txids = st.text(
    alphabet=st.characters(min_codepoint=ord("0"), max_codepoint=ord("9")),
    min_size=64, max_size=64,
).map(lambda s: "tx" + s[2:])


# ---------------------------------------------------------------------------
# 1. Single-thread invariants
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@given(key=_keys, txid=_txids)
@settings(max_examples=200, deadline=None)
def test_after_commit_subsequent_reserve_returns_same_txid(key, txid):
    """For ANY key + txid: once committed, reserve() returns the
    cached txid forever (until process restart). Never None, never
    a different txid."""
    store = IdempotencyStore()
    assert store.reserve(key) is None
    store.commit(key, txid)
    for _ in range(5):
        assert store.reserve(key) == txid


@pytest.mark.invariant
@given(key=_keys)
@settings(max_examples=200, deadline=None)
def test_release_after_reserve_lets_next_reserve_succeed(key):
    """For ANY key: reserve → release → reserve must succeed (None).
    A released slot is fully free."""
    store = IdempotencyStore()
    assert store.reserve(key) is None
    store.release(key)
    assert store.reserve(key) is None  # fresh take


@pytest.mark.invariant
@given(key=_keys, txid=_txids)
@settings(max_examples=200, deadline=None)
def test_release_after_commit_does_not_clobber(key, txid):
    """For ANY key + txid: a defensive release on already-committed
    slot must NOT erase the cached txid. (The success path of /send
    calls release() in its except: handler in case commit() itself
    raised; this must be safe.)"""
    store = IdempotencyStore()
    store.reserve(key)
    store.commit(key, txid)
    store.release(key)  # defensive call
    assert store.reserve(key) == txid


# ---------------------------------------------------------------------------
# 2. Concurrent invariants
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@given(
    key=_keys,
    n_threads=st.integers(min_value=2, max_value=10),
)
@settings(max_examples=20, deadline=None)
def test_concurrent_reserves_one_winner(key, n_threads):
    """For ANY key + thread count: under N concurrent reserve() calls
    on the same key, exactly ONE thread receives None (the slot)
    and the rest receive the committed txid (after the winner
    commits). This is THE money-safety invariant for concurrent
    /send."""
    store = IdempotencyStore()
    barrier = threading.Barrier(n_threads)
    results = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        r = store.reserve(key)
        if r is None:
            time.sleep(0.01)
            store.commit(key, "the-only-txid")
            with results_lock:
                results.append(("winner", None))
        else:
            with results_lock:
                results.append(("loser", r))

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "thread deadlocked"

    winners = [r for r in results if r[0] == "winner"]
    losers = [r for r in results if r[0] == "loser"]
    assert len(winners) == 1, (
        f"expected exactly 1 winner, got {len(winners)} for "
        f"key={key!r} n_threads={n_threads}"
    )
    assert len(losers) == n_threads - 1
    for _kind, txid in losers:
        assert txid == "the-only-txid"


# ---------------------------------------------------------------------------
# 3. Independence across keys
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@given(
    keys=st.lists(_keys, min_size=2, max_size=10, unique=True),
    txid=_txids,
)
@settings(max_examples=100, deadline=None)
def test_keys_are_independent(keys, txid):
    """For ANY set of distinct keys: committing one does NOT affect
    the state of the others. Replay protection is per-key, not
    global."""
    store = IdempotencyStore()
    # Commit only the first
    store.reserve(keys[0])
    store.commit(keys[0], txid)
    # Every other key must still be reservable
    for k in keys[1:]:
        assert store.reserve(k) is None
