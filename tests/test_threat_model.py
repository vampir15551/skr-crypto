"""Threat-model tests — one test per adversary class in docs/security.md.

Adding a new threat to the docs requires adding a test here. Removing
a threat requires deleting both the doc entry and the test, with an
ADR explaining why the threat is no longer in scope.

This file is the executable counterpart to `docs/security.md` —
without it, the security doc is just prose. With it, every claimed
defence has a regression-catching test.

Each test has:

  - The class label (`AT1`, `AT2`, ...) matching the adversary index
    in docs/security.md.
  - A docstring stating the claim from the security doc.
  - A test that fails if the claim is false today.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from skr_crypto.server import audit
from skr_crypto.server import encrypted_keystore as ks

pytestmark = pytest.mark.threat_model


# ---------------------------------------------------------------------------
# AT1 — Network attacker on the wire
# ---------------------------------------------------------------------------


class TestAT1NetworkAttacker:
    """Adversary: someone on the path between caller and service.

    Claim: cannot reach /send without the API key, cannot brute-force
    the key inside the rate limit, and timing leaks don't help."""

    @pytest.mark.invariant
    def test_unauthenticated_send_returns_401(self, client):
        """No X-API-Key header → /send is unreachable."""
        r = client.post("/api/v1/send", json={
            "to_address": "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL",
            "amount": "1",
            "idempotency_key": "no-auth",
        })
        assert r.status_code == 401

    @pytest.mark.invariant
    def test_wrong_token_returns_401(self, client):
        r = client.post("/api/v1/send",
                        headers={"X-API-Key": "definitely-not-the-token"},
                        json={
                            "to_address": "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL",
                            "amount": "1",
                            "idempotency_key": "wrong-auth",
                        })
        assert r.status_code == 401

    @pytest.mark.invariant
    def test_repeated_wrong_token_eventually_rate_limited(self, client):
        """Brute-force defense: enough wrong attempts return 429.
        We don't gate on a specific count — just that the response
        class transitions from 401 to 429 at some point."""
        # conftest sets RATE_LIMIT_MAX=100 for hermetic tests; loop
        # past that ceiling.
        codes = set()
        for i in range(150):
            r = client.post("/api/v1/send",
                            headers={"X-API-Key": f"bad-{i}"},
                            json={
                                "to_address": "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL",
                                "amount": "1",
                                "idempotency_key": f"bf-{i}",
                            })
            codes.add(r.status_code)
            if 429 in codes:
                break
        assert 429 in codes, f"150 wrong tokens never triggered 429 (got {codes})"

    @pytest.mark.invariant
    def test_auth_uses_constant_time_compare(self):
        """The auth path uses hmac.compare_digest — guards against
        timing oracles. Walks the chain: verify_api_key →
        _verify_with_scope → tokens.verify. compare_digest must
        appear somewhere on every leg."""
        import inspect

        import skr_crypto.server.security as sec
        import skr_crypto.server.tokens as tok

        # Top-level verify_api_key + the scope-aware helper it delegates to.
        src = (
            inspect.getsource(sec.verify_api_key)
            + inspect.getsource(sec._verify_with_scope)
        )
        assert "compare_digest" in src, (
            "security._verify_with_scope must use hmac.compare_digest "
            "for the legacy-AUTH_TOKEN check:\n" + src
        )

        # Per-token verify path — also constant-time per row.
        token_src = inspect.getsource(tok.TokenStore.verify)
        assert "compare_digest" in token_src, (
            "TokenStore.verify must use hmac.compare_digest:\n" + token_src
        )


# ---------------------------------------------------------------------------
# AT2 — Caller code with the API token (a buggy or malicious caller)
# ---------------------------------------------------------------------------


class TestAT2CallerWithToken:
    """Adversary: a caller that legitimately has a token but is buggy
    or compromised.

    Claim: idempotency prevents double-spend on retry; risk preflight
    blocks dangerous destinations regardless of caller intent; rate
    limit caps damage from a runaway client."""

    @pytest.mark.invariant
    def test_double_post_with_same_key_does_not_double_broadcast(
        self, client, auth_headers, mock_tron,
    ):
        """Same idempotency_key + same caller → ONE on-chain tx,
        irrespective of retry storm."""
        sent = {"count": 0, "txid": "tx-once"}

        def counting_send(_addr, _amt, fee_limit_sun=None):
            sent["count"] += 1
            return sent["txid"]

        mock_tron.send_usdt = MagicMock(side_effect=counting_send)
        body = {
            "to_address": "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL",
            "amount": "1",
            "idempotency_key": "buggy-retry-key",
        }
        for _ in range(5):
            r = client.post("/api/v1/send", headers=auth_headers, json=body)
            assert r.status_code == 200, r.text
        assert sent["count"] == 1, (
            f"buggy retry caused {sent['count']} on-chain transactions"
        )

    @pytest.mark.invariant
    def test_send_to_burn_address_is_blocked_regardless_of_caller(
        self, client, auth_headers, mock_tron,
    ):
        """A caller that asks us to send to a burn pattern is refused."""
        # The risk module catches this; we verify the network-layer
        # outcome here.
        from skr_crypto.server.risk import KNOWN_BURN_ADDRESSES
        burn = next(iter(KNOWN_BURN_ADDRESSES))
        r = client.post("/api/v1/send", headers=auth_headers, json={
            "to_address": burn,
            "amount": "1",
            "idempotency_key": "burn-attempt",
        })
        assert r.status_code == 400
        assert r.json()["code"] == "RISK_TOO_HIGH"


# ---------------------------------------------------------------------------
# AT3 — Operator with shell access on the host
# ---------------------------------------------------------------------------


class TestAT3OperatorShell:
    """Adversary: someone with shell access as the service user.

    Claim: file permissions prevent other-user disclosure; audit log
    is append-only; private key never appears in audit / logs."""

    @pytest.mark.invariant
    def test_keystore_world_read_rejected_at_load(self, tmp_path):
        """An attacker who chmod-665'd the keystore (e.g. via a
        backup-restore mistake) gets a hard refusal, not a working
        load."""
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"pw")
        os.chmod(path, 0o644)
        with pytest.raises(ks.KeystoreError, match="unsafe mode"):
            ks.load_keystore(path, b"pw")

    @pytest.mark.invariant
    def test_audit_record_does_not_contain_private_key(self, caplog):
        """Even when the test environment has the wallet's priv_key
        attached, audit.record never serialises any portion of it."""
        with caplog.at_level("INFO", logger="payouts.audit"):
            audit.record(
                "SEND_SUCCESS",
                wallet="default",
                from_address="TXxx",
                to_address="TYyy",
                amount="1",
                txid="abc",
                idempotency_key="k",
                client_ip="127.0.0.1",
                result="broadcast",
                details="elapsed=1s risk=low",
            )
        full = "\n".join(r.getMessage() for r in caplog.records)
        # The fake key is 0x11*32 — its hex form is 64 ones.
        assert "1" * 64 not in full
        assert "PrivateKey" not in full

    @pytest.mark.invariant
    def test_audit_record_does_not_contain_auth_token(self, caplog):
        from skr_crypto.server.config import AUTH_TOKEN
        with caplog.at_level("INFO", logger="payouts.audit"):
            audit.record(
                "SEND_REJECTED",
                client_ip="127.0.0.1",
                result="risk_too_high",
            )
        full = "\n".join(r.getMessage() for r in caplog.records)
        assert AUTH_TOKEN not in full


# ---------------------------------------------------------------------------
# AT4 — Root on the host
# ---------------------------------------------------------------------------


class TestAT4Root:
    """Adversary: root on the host. We don't try to defend against
    them — they own everything. But we DO claim the keystore is
    encrypted at rest, so a root-readable file is still useless
    without the passphrase."""

    @pytest.mark.invariant
    def test_keystore_file_is_aes_encrypted_not_plaintext(self, tmp_path):
        """Root can `cat keystore.json`, but should not see plaintext
        private keys — only the AES-256-GCM ciphertext."""
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"hunter2")
        ks.add_wallet(path, b"hunter2", ks.KeystoreEntry(
            name="main", address="TXxx",
            raw_key=bytearray(b"\x11" * 32),
        ))
        contents = path.read_text()
        # The plaintext hex of the key (32 1-bytes) must not appear.
        assert "11" * 32 not in contents
        # Neither should any 64-char hex run that looks like a key.
        import re
        assert not re.search(r"[a-f0-9]{64}", contents.lower())

    @pytest.mark.invariant
    def test_wrong_passphrase_indistinguishable_from_tampered_file(self, tmp_path):
        """Side-channel-resistance claim: a tampered ciphertext is
        rejected with the same error class as a wrong passphrase, so
        an attacker can't tell which one they just produced."""
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"correct")
        ks.add_wallet(path, b"correct", ks.KeystoreEntry(
            name="main", address="TXxx",
            raw_key=bytearray(b"\x11" * 32),
        ))
        # Wrong passphrase
        with pytest.raises(ks.WrongPassphrase):
            ks.load_keystore(path, b"WRONG")
        # Tampered file (flip one ciphertext byte)
        import json
        doc = json.loads(path.read_text())
        ct = doc["wallets"][0]["ciphertext_b64"]
        # Flip the first base64 char — guaranteed to corrupt
        flipped = ("X" if ct[0] != "X" else "Y") + ct[1:]
        doc["wallets"][0]["ciphertext_b64"] = flipped
        path.write_text(json.dumps(doc))
        os.chmod(path, 0o600)
        with pytest.raises(ks.WrongPassphrase):
            ks.load_keystore(path, b"correct")


# ---------------------------------------------------------------------------
# AT5 — Stolen backup tarball
# ---------------------------------------------------------------------------


class TestAT5StolenBackup:
    """Adversary: someone who acquired a backup tarball via misplaced
    backups, leaked S3 bucket, etc.

    Claim: keystore is useless without the passphrase, AUTH_TOKEN can
    be rotated independently of the keystore."""

    @pytest.mark.invariant
    def test_keystore_without_passphrase_is_unusable(self, tmp_path):
        """An attacker has the file but no passphrase — every load
        attempt fails. Non-empty wrong passphrases raise
        WrongPassphrase; the right passphrase still loads cleanly."""
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"pwd")
        ks.add_wallet(path, b"pwd", ks.KeystoreEntry(
            name="main", address="TXxx",
            raw_key=bytearray(b"\x11" * 32),
        ))
        for guess in [b"pwd1", b"password", b"hunter2", b"123456",
                      b"\x00" * 16, b"x"]:
            with pytest.raises(ks.WrongPassphrase):
                ks.load_keystore(path, guess)
        # And the right one still works:
        entries = ks.load_keystore(path, b"pwd")
        assert len(entries) == 1
        assert bytes(entries[0].raw_key) == b"\x11" * 32


# ---------------------------------------------------------------------------
# AT6 — Compromised dependency
# ---------------------------------------------------------------------------


class TestAT6CompromisedDependency:
    """Adversary: a malicious upstream bump in tronpy / cryptography /
    requests / etc.

    Claim: pinned ranges + Dependabot visibility + pip-audit gating
    catch known CVEs. We can't fully test "an unknown supply-chain
    attack" but we can assert the CI gate is wired up."""

    @pytest.mark.invariant
    def test_pyproject_pins_cryptography_above_known_cves(self):
        """The cryptography lower bound must skip the 1.4.0 CVE set
        (45.x). Found via pip-audit; documented in CHANGELOG."""
        import pathlib
        import tomllib
        pyproject = pathlib.Path(__file__).parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text())
        deps = data["project"]["optional-dependencies"]["server"]
        crypto = next(d for d in deps if d.lower().startswith("cryptography"))
        # Lower bound must be at least 46.0.7 — the version that fixes
        # CVE-2026-26007, CVE-2026-34073, and CVE-2026-39892.
        # If you raise the lower bound, update this test along with it.
        assert ">=46.0.7" in crypto or ">=46.0.8" in crypto or ">=46.1" in crypto, (
            f"cryptography pin is {crypto!r} — must pin >=46.0.7 to skip 45.x CVEs"
        )

    @pytest.mark.invariant
    def test_pyproject_python_minimum_is_311(self):
        """Python 3.11 is the floor — older versions don't have the
        type hints we use."""
        import pathlib
        import tomllib
        pyproject = pathlib.Path(__file__).parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text())
        assert data["project"]["requires-python"].startswith(">=3.11"), (
            "Python minimum dropped — verify all syntax still works"
        )
