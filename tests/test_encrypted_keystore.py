"""Tests for the AES-256-GCM keystore (1.4.0).

Covers: round-trip encrypt/decrypt, wrong-passphrase rejection, file-mode
gates, idempotent add/remove/rename, atomic write semantics, and the
passphrase resolution chain.
"""
from __future__ import annotations

import os
import stat

import pytest

from skr_crypto.server import encrypted_keystore as ks

VALID_KEY_A = bytearray(b"\x11" * 32)
VALID_KEY_B = bytearray(b"\x22" * 32)


# ---------------------------------------------------------------------------
# init / load round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_init_creates_chmod_600_file(self, tmp_path):
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"hunter2")
        assert path.exists()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {mode:#o}"
        # Empty wallets list, but the file is valid.
        assert ks.list_wallet_names(path) == []

    def test_load_after_init_returns_no_wallets(self, tmp_path):
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"hunter2")
        entries = ks.load_keystore(path, b"hunter2")
        assert entries == []

    def test_add_then_load_returns_same_key(self, tmp_path):
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"pw")
        ks.add_wallet(path, b"pw", ks.KeystoreEntry(
            name="main", address="TXxx", raw_key=bytearray(VALID_KEY_A),
        ))
        entries = ks.load_keystore(path, b"pw")
        assert len(entries) == 1
        assert entries[0].name == "main"
        assert entries[0].address == "TXxx"
        assert bytes(entries[0].raw_key) == bytes(VALID_KEY_A)

    def test_two_wallets_round_trip_independently(self, tmp_path):
        """Different IVs per wallet — the same plaintext key would still
        produce different ciphertext blobs."""
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"pw")
        ks.add_wallet(path, b"pw", ks.KeystoreEntry(
            name="a", address="TA", raw_key=bytearray(VALID_KEY_A),
        ))
        ks.add_wallet(path, b"pw", ks.KeystoreEntry(
            name="b", address="TB", raw_key=bytearray(VALID_KEY_B),
        ))
        entries = ks.load_keystore(path, b"pw")
        names = {e.name: bytes(e.raw_key) for e in entries}
        assert names == {"a": bytes(VALID_KEY_A), "b": bytes(VALID_KEY_B)}


# ---------------------------------------------------------------------------
# Passphrase / corruption rejection
# ---------------------------------------------------------------------------


class TestPassphrase:
    @pytest.mark.invariant
    def test_wrong_passphrase_raises_clean_error(self, tmp_path):
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"correct")
        ks.add_wallet(path, b"correct", ks.KeystoreEntry(
            name="main", address="T", raw_key=bytearray(VALID_KEY_A),
        ))
        with pytest.raises(ks.WrongPassphrase):
            ks.load_keystore(path, b"WRONG")

    def test_init_refuses_empty_passphrase(self, tmp_path):
        with pytest.raises(ks.KeystoreError, match="empty"):
            ks.init_keystore(tmp_path / "ks.json", b"")

    @pytest.mark.invariant
    def test_save_refuses_zero_key(self, tmp_path):
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"pw")
        with pytest.raises(ks.KeystoreError, match="zeroes"):
            ks.add_wallet(path, b"pw", ks.KeystoreEntry(
                name="zero", address="", raw_key=bytearray(32),  # all zeros
            ))


# ---------------------------------------------------------------------------
# File-mode gate
# ---------------------------------------------------------------------------


class TestFileMode:
    @pytest.mark.invariant
    def test_world_readable_keystore_rejected(self, tmp_path):
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"pw")
        os.chmod(path, 0o644)  # group + other readable
        with pytest.raises(ks.KeystoreError, match="unsafe mode"):
            ks.load_keystore(path, b"pw")

    def test_group_readable_keystore_rejected(self, tmp_path):
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"pw")
        os.chmod(path, 0o640)
        with pytest.raises(ks.KeystoreError, match="unsafe mode"):
            ks.load_keystore(path, b"pw")

    def test_missing_keystore_raises_KeystoreNotFound(self, tmp_path):
        with pytest.raises(ks.KeystoreNotFound):
            ks.load_keystore(tmp_path / "does-not-exist.json", b"pw")


# ---------------------------------------------------------------------------
# add / remove / rename
# ---------------------------------------------------------------------------


class TestMutations:
    def _seed(self, tmp_path):
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"pw")
        ks.add_wallet(path, b"pw", ks.KeystoreEntry(
            name="main", address="TM", raw_key=bytearray(VALID_KEY_A),
        ))
        ks.add_wallet(path, b"pw", ks.KeystoreEntry(
            name="reserve", address="TR", raw_key=bytearray(VALID_KEY_B),
        ))
        return path

    def test_add_duplicate_name_raises(self, tmp_path):
        path = self._seed(tmp_path)
        with pytest.raises(ks.WalletNameConflict):
            ks.add_wallet(path, b"pw", ks.KeystoreEntry(
                name="main", address="X", raw_key=bytearray(VALID_KEY_A),
            ))

    def test_remove_returns_entry_and_removes(self, tmp_path):
        path = self._seed(tmp_path)
        removed = ks.remove_wallet(path, b"pw", "main")
        assert removed.name == "main"
        names = [e.name for e in ks.load_keystore(path, b"pw")]
        assert names == ["reserve"]

    def test_remove_unknown_raises(self, tmp_path):
        path = self._seed(tmp_path)
        with pytest.raises(ks.KeystoreError, match="not in keystore"):
            ks.remove_wallet(path, b"pw", "ghost")

    def test_rename_changes_aad_and_round_trips(self, tmp_path):
        """The wallet name is bound into AES-GCM AAD — renaming must
        re-encrypt with the new AAD or load would fail authentication."""
        path = self._seed(tmp_path)
        ks.rename_wallet(path, b"pw", "main", "treasury")
        entries = ks.load_keystore(path, b"pw")
        names = sorted(e.name for e in entries)
        assert names == ["reserve", "treasury"]

    def test_rename_to_existing_name_raises(self, tmp_path):
        path = self._seed(tmp_path)
        with pytest.raises(ks.WalletNameConflict):
            ks.rename_wallet(path, b"pw", "main", "reserve")


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_init_refuses_to_overwrite(self, tmp_path):
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"pw")
        with pytest.raises(ks.KeystoreError, match="already exists"):
            ks.init_keystore(path, b"pw")

    def test_init_overwrite_flag_replaces(self, tmp_path):
        path = tmp_path / "ks.json"
        ks.init_keystore(path, b"old-pw")
        ks.add_wallet(path, b"old-pw", ks.KeystoreEntry(
            name="x", address="T", raw_key=bytearray(VALID_KEY_A),
        ))
        # Re-init with new passphrase wipes wallets + rotates salt.
        ks.init_keystore(path, b"new-pw", overwrite=True)
        # Old passphrase no longer decrypts (the keystore is now empty,
        # but more importantly, adding a wallet under the old passphrase
        # would fail because the salt + derived key changed).
        ks.add_wallet(path, b"new-pw", ks.KeystoreEntry(
            name="y", address="T", raw_key=bytearray(VALID_KEY_B),
        ))
        with pytest.raises(ks.WrongPassphrase):
            ks.load_keystore(path, b"old-pw")
        assert [e.name for e in ks.load_keystore(path, b"new-pw")] == ["y"]


# ---------------------------------------------------------------------------
# Passphrase resolution
# ---------------------------------------------------------------------------


class TestResolvePassphrase:
    def test_env_var_value_wins(self, tmp_path):
        out = ks.resolve_passphrase(env_var_value="from-env")
        assert out == b"from-env"

    def test_file_path_used_when_env_empty(self, tmp_path):
        p = tmp_path / "pp"
        p.write_bytes(b"file-secret\n")
        os.chmod(p, 0o600)
        assert ks.resolve_passphrase(file_path=p) == b"file-secret"

    def test_file_must_be_chmod_600(self, tmp_path):
        p = tmp_path / "pp"
        p.write_bytes(b"x")
        os.chmod(p, 0o644)
        with pytest.raises(ks.KeystoreError, match="unsafe mode"):
            ks.resolve_passphrase(file_path=p)

    def test_no_source_raises(self, tmp_path):
        with pytest.raises(ks.KeystoreError, match="no passphrase source"):
            ks.resolve_passphrase()
