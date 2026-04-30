"""Tests for the per-caller token system (1.5.0).

Covers: scrypt-hashed storage, scope hierarchy, legacy AUTH_TOKEN
compat, revocation, rotation, and the integration with /api/v1/*.
"""
from __future__ import annotations

import pytest

from skr_crypto.server.tokens import (
    ALL_SCOPES,
    InsufficientScope,
    TokenError,
    TokenInvalid,
    TokenRevoked,
    TokenStore,
    _expand_scopes,
    _scope_satisfies,
    generate_token,
)

# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


class TestTokenGeneration:
    def test_token_has_recognizable_prefix(self):
        t = generate_token()
        assert t.startswith("skr_")

    def test_token_has_sufficient_entropy(self):
        # 32 random bytes → urlsafe-base64 ≈ 43 chars + prefix
        seen = {generate_token() for _ in range(100)}
        assert len(seen) == 100, "duplicates in 100-token sample"


# ---------------------------------------------------------------------------
# Scope hierarchy
# ---------------------------------------------------------------------------


class TestScopeHierarchy:
    def test_admin_implies_everything(self):
        expanded = _expand_scopes(frozenset({"admin"}))
        assert expanded == ALL_SCOPES

    def test_send_implies_read(self):
        assert _scope_satisfies(frozenset({"send"}), "read") is True

    def test_read_does_not_imply_send(self):
        assert _scope_satisfies(frozenset({"read"}), "send") is False

    def test_metrics_isolated_from_read(self):
        assert _scope_satisfies(frozenset({"metrics"}), "read") is False
        assert _scope_satisfies(frozenset({"read"}), "metrics") is False

    def test_admin_satisfies_metrics(self):
        assert _scope_satisfies(frozenset({"admin"}), "metrics") is True


# ---------------------------------------------------------------------------
# Store: in-memory mode
# ---------------------------------------------------------------------------


class TestTokenStoreInMemory:
    def _store(self) -> TokenStore:
        return TokenStore(db_path=None)

    def test_create_returns_record_and_plaintext(self):
        store = self._store()
        rec, plaintext = store.create(name="test", scopes=["send", "read"])
        assert rec.name == "test"
        assert rec.scopes == frozenset({"send", "read"})
        assert plaintext.startswith("skr_")
        assert rec.revoked_at is None

    @pytest.mark.invariant
    def test_plaintext_token_is_not_stored(self):
        """INVARIANT: the plaintext token MUST NOT be persisted in any
        form. Only its scrypt-derived hash + salt. If this regresses,
        an attacker with DB read access gets every active token."""
        store = self._store()
        _, plaintext = store.create(name="x", scopes=["read"])
        # In-memory mode stores in self._memory dict — verify the token
        # value isn't in any field.
        for d in store._memory.values():
            for v in d.values():
                if isinstance(v, str):
                    assert plaintext not in v
                if isinstance(v, bytes):
                    assert plaintext.encode() not in v

    def test_create_rejects_empty_name(self):
        store = self._store()
        with pytest.raises(TokenError):
            store.create(name="", scopes=["read"])

    def test_create_rejects_unknown_scope(self):
        store = self._store()
        with pytest.raises(TokenError):
            store.create(name="x", scopes=["nuclear-launch"])

    def test_create_rejects_empty_scopes(self):
        store = self._store()
        with pytest.raises(TokenError):
            store.create(name="x", scopes=[])

    @pytest.mark.invariant
    def test_verify_returns_id_on_match(self):
        """INVARIANT: a valid plaintext + sufficient scope returns the
        token id. Any other behaviour means scope or auth is broken."""
        store = self._store()
        rec, plaintext = store.create(name="t1", scopes=["send", "read"])
        assert store.verify(plaintext, required_scope="read") == rec.id
        assert store.verify(plaintext, required_scope="send") == rec.id

    @pytest.mark.invariant
    def test_verify_rejects_insufficient_scope(self):
        """INVARIANT: a read-only token MUST NOT pass a send scope check.
        This is the whole point of scopes."""
        store = self._store()
        _, plaintext = store.create(name="ro", scopes=["read"])
        with pytest.raises(InsufficientScope):
            store.verify(plaintext, required_scope="send")

    @pytest.mark.invariant
    def test_verify_rejects_unknown_token(self):
        """INVARIANT: random-looking-but-not-issued tokens fail."""
        store = self._store()
        store.create(name="other", scopes=["send"])
        with pytest.raises(TokenInvalid):
            store.verify("skr_random-but-fake-token-value", required_scope="read")

    def test_verify_rejects_empty_token(self):
        store = self._store()
        with pytest.raises(TokenInvalid):
            store.verify("", required_scope="read")

    @pytest.mark.invariant
    def test_revoked_token_rejected(self):
        """INVARIANT: revocation MUST take effect on the next request.
        If a revoked token still validates, the rotation primitive is
        broken."""
        store = self._store()
        rec, plaintext = store.create(name="leaked", scopes=["send"])
        # Works before revocation
        assert store.verify(plaintext, required_scope="send") == rec.id
        store.revoke(rec.id)
        with pytest.raises(TokenRevoked):
            store.verify(plaintext, required_scope="send")

    def test_revoke_is_idempotent(self):
        store = self._store()
        rec, _ = store.create(name="x", scopes=["read"])
        store.revoke(rec.id)
        store.revoke(rec.id)  # no-op, no exception
        store.revoke("nonexistent-id")  # no-op too

    def test_list_default_excludes_revoked(self):
        store = self._store()
        a, _ = store.create(name="a", scopes=["read"])
        _b, _ = store.create(name="b", scopes=["read"])
        store.revoke(a.id)
        active = store.list()
        names = {r.name for r in active}
        assert names == {"b"}

    def test_list_include_revoked(self):
        store = self._store()
        a, _ = store.create(name="a", scopes=["read"])
        store.create(name="b", scopes=["read"])
        store.revoke(a.id)
        all_rows = store.list(include_revoked=True)
        assert len(all_rows) == 2

    def test_last_used_at_updates_on_verify(self):
        store = self._store()
        rec, plaintext = store.create(name="x", scopes=["read"])
        assert rec.last_used_at is None
        store.verify(plaintext, required_scope="read")
        # Re-fetch
        rec2 = store.get(rec.id)
        assert rec2.last_used_at is not None


# ---------------------------------------------------------------------------
# Store: SQLite-backed mode
# ---------------------------------------------------------------------------


class TestTokenStoreSqlite:
    def test_round_trip_via_sqlite(self, tmp_path):
        db = tmp_path / "tokens.db"
        store = TokenStore(db_path=db)
        rec, plaintext = store.create(name="prod", scopes=["send", "read"])
        store.close()

        # Open a new store from the same file — the token should be
        # there and verify cleanly.
        store2 = TokenStore(db_path=db)
        assert store2.verify(plaintext, required_scope="send") == rec.id
        store2.close()

    @pytest.mark.invariant
    def test_revocation_persists_across_restart(self, tmp_path):
        """INVARIANT: revoking a token in run #1 must reject it in
        run #2. In-memory revocation is useless if state vanishes."""
        db = tmp_path / "tokens.db"
        store = TokenStore(db_path=db)
        rec, plaintext = store.create(name="leaked", scopes=["send"])
        store.revoke(rec.id)
        store.close()

        store2 = TokenStore(db_path=db)
        with pytest.raises(TokenRevoked):
            store2.verify(plaintext, required_scope="send")
        store2.close()


# ---------------------------------------------------------------------------
# HTTP integration with the /api/v1 endpoints
# ---------------------------------------------------------------------------


class TestTokenIntegrationWithRoutes:
    """The conftest fixture sets AUTH_TOKEN=test-secret-token; that
    legacy path stays admin-equivalent. We also exercise scope
    enforcement with explicit per-scope tokens."""

    def _create_token(self, scopes):
        from skr_crypto.server.tokens import tokens as global_store
        global_store._reset_for_tests()
        _rec, plaintext = global_store.create(name="t", scopes=scopes)
        return plaintext

    def test_legacy_auth_token_still_works_for_send(
        self, client, auth_headers, mock_tron,
    ):
        """Existing /send tests use the conftest auth_headers fixture
        which carries the legacy AUTH_TOKEN. Must keep working."""
        resp = client.post("/api/v1/send", headers=auth_headers, json={
            "to_address": "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL",
            "amount": "1",
            "idempotency_key": "legacy-compat-1",
        })
        assert resp.status_code == 200, resp.text

    def test_scoped_send_token_works_for_send(self, client, mock_tron):
        plaintext = self._create_token(["send", "read"])
        resp = client.post(
            "/api/v1/send",
            headers={"X-API-Key": plaintext},
            json={
                "to_address": "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL",
                "amount": "1",
                "idempotency_key": "scoped-send-1",
            },
        )
        assert resp.status_code == 200, resp.text

    @pytest.mark.invariant
    def test_read_only_token_cannot_send(self, client, mock_tron):
        """INVARIANT: a read-only token returns 403 INSUFFICIENT_SCOPE
        on /send. If this regresses, the whole point of scopes is
        defeated — every token can move money."""
        plaintext = self._create_token(["read"])
        resp = client.post(
            "/api/v1/send",
            headers={"X-API-Key": plaintext},
            json={
                "to_address": "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL",
                "amount": "1",
                "idempotency_key": "read-only-blocked",
            },
        )
        assert resp.status_code == 403
        # FastAPI wraps the dict in `detail` since we raised HTTPException
        # with a dict — verify the code field comes through somehow.
        body = resp.json()
        # Either flat error/code OR detail.code shape — either is fine
        # as long as the code is preserved.
        text = str(body)
        assert "INSUFFICIENT_SCOPE" in text

    def test_metrics_only_token_cannot_read_balance(self, client, mock_tron):
        """A token with only `metrics` scope can scrape /metrics but
        not see balances."""
        plaintext = self._create_token(["metrics"])
        resp = client.get(
            "/api/v1/balance",
            headers={"X-API-Key": plaintext},
        )
        assert resp.status_code == 403

    def test_metrics_only_token_can_scrape_metrics(self, client, mock_tron):
        plaintext = self._create_token(["metrics"])
        resp = client.get(
            "/api/v1/metrics",
            headers={"X-API-Key": plaintext},
        )
        assert resp.status_code == 200

    def test_admin_token_can_do_everything(self, client, mock_tron):
        plaintext = self._create_token(["admin"])
        # send
        r1 = client.post("/api/v1/send",
                         headers={"X-API-Key": plaintext},
                         json={
                             "to_address": "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL",
                             "amount": "1",
                             "idempotency_key": "admin-can-send",
                         })
        assert r1.status_code == 200, r1.text
        # read
        r2 = client.get("/api/v1/balance", headers={"X-API-Key": plaintext})
        assert r2.status_code == 200
        # metrics
        r3 = client.get("/api/v1/metrics", headers={"X-API-Key": plaintext})
        assert r3.status_code == 200

    def test_unauthenticated_returns_401(self, client):
        resp = client.post("/api/v1/send", json={
            "to_address": "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL",
            "amount": "1",
            "idempotency_key": "no-auth",
        })
        assert resp.status_code == 401

    def test_revoked_token_returns_401(self, client, mock_tron):
        from skr_crypto.server.tokens import tokens as global_store
        global_store._reset_for_tests()
        rec, plaintext = global_store.create(name="will-revoke", scopes=["send"])
        global_store.revoke(rec.id)
        resp = client.post("/api/v1/send",
                           headers={"X-API-Key": plaintext},
                           json={
                               "to_address": "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL",
                               "amount": "1",
                               "idempotency_key": "revoked-blocked",
                           })
        assert resp.status_code == 401
