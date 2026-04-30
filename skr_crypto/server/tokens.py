"""Per-caller API tokens with scopes.

See ADR 0008. Each token is 32 random bytes (256 bits), stored as a
scrypt-derived hash + per-token salt. Verification is constant-time
per row. Tokens carry one or more **scopes** that gate which endpoints
they can call.

Scope hierarchy:

    admin   ⊇ send ⊇ read
    admin   ⊇ metrics
    metrics is independent of read

In words: an `admin` token can do anything. A `send` token can also
read. A `read` token cannot send. A `metrics` token can only scrape
`/metrics`. A token can have multiple scopes (e.g. `read,metrics`).

Backward compat: the legacy ``AUTH_TOKEN`` env var, if set, is honoured
as a synthetic admin token with id ``legacy``. A loud warning is
logged on first use and on boot.

Storage: a `tokens` table in the same SQLite database as the
idempotency store (or a fresh one if `IDEMPOTENCY_DB_PATH` is unset —
in that case the in-memory variant is used and tokens vanish on
restart, which is acceptable for dev only).
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

log = logging.getLogger("payouts")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Token format: skr_<43 base64url chars> = ~256 bits of entropy.
# The prefix lets accidental-paste detectors (Gitleaks, GitHub
# secret-scanning) flag it.
TOKEN_PREFIX = "skr_"
TOKEN_RANDOM_BYTES = 32

# Per-token scrypt parameters. Lighter than the keystore's KDF —
# this hash runs on every authenticated request, not once per
# process. ~5 ms / row on modern hardware.
SCRYPT_N = 4096
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LEN = 32
SCRYPT_SALT_LEN = 16

# Known scopes. Validated at create-time.
ALL_SCOPES = frozenset({"admin", "send", "read", "metrics"})

# Implication graph: having key implies having values.
_IMPLIES = {
    "admin":   frozenset({"admin", "send", "read", "metrics"}),
    "send":    frozenset({"send", "read"}),
    "read":    frozenset({"read"}),
    "metrics": frozenset({"metrics"}),
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TokenError(Exception):
    """Base class for token-related errors."""


class TokenInvalid(TokenError):
    """Supplied token doesn't match any record."""


class TokenRevoked(TokenError):
    """Token matches but has been revoked."""


class InsufficientScope(TokenError):
    """Token is valid but lacks the required scope."""

    def __init__(self, required: str, has: list[str]):
        self.required = required
        self.has = list(has)
        super().__init__(
            f"token requires scope {required!r}; this token has {sorted(has)!r}"
        )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TokenRecord:
    """One row in the `tokens` table. Plaintext token never stored."""

    id: str                  # short uuid
    name: str                # operator label
    scopes: frozenset[str]
    created_at: str          # ISO 8601 UTC
    last_used_at: str | None
    revoked_at: str | None
    created_by: str          # token id of the creator, or "AUTH_TOKEN-legacy"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _validate_scopes(scopes: list[str]) -> frozenset[str]:
    """Return the canonical normalized scope set, or raise."""
    if not scopes:
        raise TokenError("token must have at least one scope")
    out = set()
    for s in scopes:
        s = s.strip().lower()
        if s not in ALL_SCOPES:
            raise TokenError(
                f"unknown scope {s!r} (valid: {sorted(ALL_SCOPES)})"
            )
        out.add(s)
    return frozenset(out)


def _expand_scopes(scopes: frozenset[str]) -> frozenset[str]:
    """Apply implication graph: e.g. admin → {admin, send, read, metrics}."""
    expanded = set()
    for s in scopes:
        expanded |= _IMPLIES.get(s, frozenset({s}))
    return frozenset(expanded)


def _hash_token(plaintext: str, salt: bytes) -> bytes:
    """scrypt-derive a 32-byte hash. One-shot KDF instance per call."""
    kdf = Scrypt(salt=salt, length=SCRYPT_LEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(plaintext.encode("utf-8"))


def generate_token() -> str:
    """Mint a fresh token. 256 bits of entropy with a recognizable prefix."""
    raw = secrets.token_urlsafe(TOKEN_RANDOM_BYTES)
    return f"{TOKEN_PREFIX}{raw}"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    hash         BLOB NOT NULL,
    salt         BLOB NOT NULL,
    scopes       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT,
    created_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_revoked ON tokens(revoked_at);
"""


class TokenStore:
    """Persistent token store. Uses SQLite WAL when a path is given;
    in-memory dict otherwise (dev / test only).

    Verification is O(N) over the active (non-revoked) tokens. The
    constant-time per-row compare prevents timing leaks. With operator-
    managed N (typically <20), this is well under the latency budget
    of any /send call.
    """

    def __init__(self, db_path: str | Path | None = None):
        self._lock = threading.RLock()
        self._db_path = db_path
        if db_path:
            # SQLite connection; WAL mode for concurrent read+write.
            self._conn = sqlite3.connect(
                str(db_path), check_same_thread=False, isolation_level=None,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._memory: dict[str, dict] | None = None
        else:
            # In-memory mode for dev / tests.
            self._conn = None
            self._memory = {}

    # -- internal: row → dict and back --------------------------------------

    def _row_to_dict(self, row) -> dict:
        return {
            "id": row[0],
            "name": row[1],
            "hash": row[2],
            "salt": row[3],
            "scopes": frozenset(row[4].split(",")),
            "created_at": row[5],
            "last_used_at": row[6],
            "revoked_at": row[7],
            "created_by": row[8],
        }

    def _persist(self, d: dict) -> None:
        if self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO tokens "
                "(id, name, hash, salt, scopes, created_at, last_used_at, revoked_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (d["id"], d["name"], d["hash"], d["salt"],
                 ",".join(sorted(d["scopes"])),
                 d["created_at"], d["last_used_at"], d["revoked_at"],
                 d["created_by"]),
            )
        else:
            self._memory[d["id"]] = dict(d)

    def _all_active(self) -> list[dict]:
        if self._conn:
            cur = self._conn.execute(
                "SELECT id, name, hash, salt, scopes, created_at, last_used_at, "
                "revoked_at, created_by FROM tokens WHERE revoked_at IS NULL"
            )
            return [self._row_to_dict(r) for r in cur]
        else:
            return [d for d in self._memory.values() if not d["revoked_at"]]

    def _all(self) -> list[dict]:
        if self._conn:
            cur = self._conn.execute(
                "SELECT id, name, hash, salt, scopes, created_at, last_used_at, "
                "revoked_at, created_by FROM tokens ORDER BY created_at"
            )
            return [self._row_to_dict(r) for r in cur]
        else:
            return list(self._memory.values())

    # -- public API ---------------------------------------------------------

    def create(
        self,
        name: str,
        scopes: list[str],
        *,
        created_by: str = "operator",
    ) -> tuple[TokenRecord, str]:
        """Mint a new token. Returns (record, plaintext_token).

        The plaintext token is shown ONCE to the operator and never
        stored. Lose it = mint a new one and revoke this one.
        """
        if not name or not name.strip():
            raise TokenError("token name must not be empty")
        canon_scopes = _validate_scopes(scopes)
        plaintext = generate_token()
        salt = secrets.token_bytes(SCRYPT_SALT_LEN)
        token_hash = _hash_token(plaintext, salt)
        token_id = uuid.uuid4().hex[:12]
        record = {
            "id": token_id,
            "name": name.strip(),
            "hash": token_hash,
            "salt": salt,
            "scopes": canon_scopes,
            "created_at": _now_iso(),
            "last_used_at": None,
            "revoked_at": None,
            "created_by": created_by,
        }
        with self._lock:
            self._persist(record)
        log.info(
            "[TOKEN] created id=%s name=%r scopes=%s by=%s",
            token_id, name, sorted(canon_scopes), created_by,
        )
        return self._to_record(record), plaintext

    def revoke(self, token_id: str) -> None:
        """Mark a token as revoked. Idempotent."""
        with self._lock:
            if self._conn:
                self._conn.execute(
                    "UPDATE tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (_now_iso(), token_id),
                )
            else:
                if token_id in self._memory:
                    self._memory[token_id]["revoked_at"] = _now_iso()
        log.info("[TOKEN] revoked id=%s", token_id)

    def list(self, *, include_revoked: bool = False) -> list[TokenRecord]:
        """Return every token's metadata. The hash + salt are not
        included in the public TokenRecord."""
        with self._lock:
            rows = self._all() if include_revoked else self._all_active()
        return [self._to_record(d) for d in rows]

    def get(self, token_id: str) -> TokenRecord | None:
        with self._lock:
            for d in self._all():
                if d["id"] == token_id:
                    return self._to_record(d)
        return None

    def verify(self, plaintext: str, *, required_scope: str) -> str:
        """Validate a plaintext token and check its scope.

        Returns the token's id (or "legacy" for AUTH_TOKEN fallback)
        on success.

        Raises:
            TokenInvalid       — no record matches
            TokenRevoked       — record matches but is revoked
            InsufficientScope  — record valid but lacks required scope

        The lookup is O(N) over active records. We hash the supplied
        plaintext with EACH active row's salt and compare in constant
        time; this defeats timing attacks against an attacker who has
        partial knowledge of the salt set.
        """
        if not plaintext:
            raise TokenInvalid("empty token")

        with self._lock:
            active = self._all_active()
            # If admin is supplied via legacy AUTH_TOKEN, short-circuit.
            legacy = _check_legacy_token(plaintext)
            if legacy:
                if not _scope_satisfies(frozenset({"admin"}), required_scope):
                    # admin satisfies anything; this branch is unreachable
                    # but kept for symmetry.
                    raise InsufficientScope(required_scope, ["admin"])
                return "legacy"

            for d in active:
                candidate_hash = _hash_token(plaintext, d["salt"])
                if hmac.compare_digest(candidate_hash, d["hash"]):
                    # Match. Check scope.
                    if not _scope_satisfies(d["scopes"], required_scope):
                        raise InsufficientScope(required_scope, list(d["scopes"]))
                    # Update last_used_at (best-effort; failure here
                    # shouldn't fail the request).
                    try:
                        if self._conn:
                            self._conn.execute(
                                "UPDATE tokens SET last_used_at=? WHERE id=?",
                                (_now_iso(), d["id"]),
                            )
                        else:
                            self._memory[d["id"]]["last_used_at"] = _now_iso()
                    except Exception as exc:
                        log.warning("[TOKEN] last_used_at update failed: %s", exc)
                    return d["id"]

        # No active match. Check if this matches a revoked one for
        # better error messaging.
        with self._lock:
            for d in self._all():
                if d["revoked_at"]:
                    candidate_hash = _hash_token(plaintext, d["salt"])
                    if hmac.compare_digest(candidate_hash, d["hash"]):
                        raise TokenRevoked(
                            f"token id={d['id']} was revoked at {d['revoked_at']}"
                        )

        raise TokenInvalid("token does not match any record")

    # -- helpers ------------------------------------------------------------

    def _to_record(self, d: dict) -> TokenRecord:
        return TokenRecord(
            id=d["id"],
            name=d["name"],
            scopes=d["scopes"],
            created_at=d["created_at"],
            last_used_at=d["last_used_at"],
            revoked_at=d["revoked_at"],
            created_by=d["created_by"],
        )

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def _reset_for_tests(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.execute("DELETE FROM tokens")
            elif self._memory is not None:
                self._memory.clear()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _scope_satisfies(token_scopes: frozenset[str], required: str) -> bool:
    """Does ``token_scopes`` (post-implication-expansion) include ``required``?"""
    expanded = _expand_scopes(token_scopes)
    return required in expanded


def _check_legacy_token(plaintext: str) -> bool:
    """Compare against AUTH_TOKEN env var (constant-time)."""
    legacy = os.environ.get("AUTH_TOKEN", "")
    if not legacy:
        return False
    return hmac.compare_digest(plaintext, legacy)


# ---------------------------------------------------------------------------
# Singleton (initialised by server lifespan)
# ---------------------------------------------------------------------------


tokens = TokenStore(db_path=None)  # default in-memory; overridden at boot
_legacy_warned = False


def init_token_store(db_path: str | Path | None) -> None:
    """Replace the module-level singleton with a path-backed store.

    Called once at lifespan boot, after IDEMPOTENCY_DB_PATH is known.
    Idempotent for the test path (re-init is fine, fresh in-memory
    table on each pytest run).
    """
    global tokens
    if tokens is not None:
        try:
            tokens.close()
        except Exception:
            pass
    tokens = TokenStore(db_path=db_path)
    log.info(
        "[TOKEN] store initialised (%s)",
        "path=" + str(db_path) if db_path else "in-memory",
    )


def warn_legacy_once() -> None:
    """Log the legacy-AUTH_TOKEN deprecation warning once per process."""
    global _legacy_warned
    if _legacy_warned:
        return
    _legacy_warned = True
    if os.environ.get("AUTH_TOKEN"):
        log.warning(
            "[AUTH] Legacy AUTH_TOKEN env var is in use as an admin "
            "token (id=legacy). This works for backwards compatibility "
            "but does not produce per-caller audit attribution. "
            "Migrate to scoped tokens via `skr-crypto token create`. "
            "AUTH_TOKEN will be removed in 2.0."
        )
