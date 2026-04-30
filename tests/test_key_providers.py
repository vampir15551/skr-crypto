"""Tests for the pluggable secret backends."""
from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Round-trip a known 32-byte test key. NEVER use this for real money — it's
# generated specifically for tests and is publicly readable in the repo.
TEST_HEX = "11" * 32  # 32 bytes of 0x11
TEST_BYTES = bytes.fromhex(TEST_HEX)


# ---------------------------------------------------------------------------
# _decode_hex_to_bytes — common path for all providers
# ---------------------------------------------------------------------------


def _import_kp():
    """Reload config (so PRIVATE_KEY_FILE / KEY_PROVIDER env vars are picked
    up) then reload key_providers so its top-level constants are refreshed
    too. Each test that mutates env vars must call this after the
    monkeypatch."""
    import skr_crypto.server.config as cfg
    importlib.reload(cfg)
    import skr_crypto.server.key_providers as kp
    importlib.reload(kp)
    return kp


class TestDecodeHex:
    def test_strips_trailing_whitespace(self):
        kp = _import_kp()
        out = kp._decode_hex_to_bytes(bytearray(TEST_HEX.encode() + b"\n"), "test")
        assert bytes(out) == TEST_BYTES

    def test_strips_0x_prefix(self):
        kp = _import_kp()
        out = kp._decode_hex_to_bytes(bytearray(b"0x" + TEST_HEX.encode()), "test")
        assert bytes(out) == TEST_BYTES

    def test_strips_0X_prefix(self):
        kp = _import_kp()
        out = kp._decode_hex_to_bytes(bytearray(b"0X" + TEST_HEX.encode()), "test")
        assert bytes(out) == TEST_BYTES

    def test_empty_input_exits(self):
        kp = _import_kp()
        with pytest.raises(SystemExit):
            kp._decode_hex_to_bytes(bytearray(b""), "test")

    def test_invalid_hex_exits(self):
        kp = _import_kp()
        with pytest.raises(SystemExit):
            kp._decode_hex_to_bytes(bytearray(b"NOT_HEX"), "test")

    def test_wrong_length_exits(self):
        kp = _import_kp()
        with pytest.raises(SystemExit):
            kp._decode_hex_to_bytes(bytearray(b"1234"), "test")

    def test_error_message_does_not_leak_key_bytes(self, caplog):
        kp = _import_kp()
        with caplog.at_level("ERROR", logger="payouts"), pytest.raises(SystemExit):
            kp._decode_hex_to_bytes(bytearray(b"NOT_HEX_DATA"), "test")
        # The error MUST NOT echo back any of the input bytes — even a prefix
        # could leak data on a misconfiguration (e.g. real key hex pasted).
        for record in caplog.records:
            assert b"NOT_HEX_DATA".decode() not in record.message
            assert "NOT_HEX_DATA" not in record.message


# ---------------------------------------------------------------------------
# EnvKeyProvider
# ---------------------------------------------------------------------------


class TestEnvKeyProvider:
    def test_reads_and_clears_env_var(self, monkeypatch):
        monkeypatch.setenv("PRIVATE_KEY_HEX", TEST_HEX)
        kp = _import_kp()
        provider = kp.EnvKeyProvider()
        key = provider.load_wallets()[0].raw_key
        assert bytes(key) == TEST_BYTES
        # Env var must be deleted after read so a child process can't inherit it.
        assert "PRIVATE_KEY_HEX" not in os.environ

    def test_missing_env_exits(self, monkeypatch):
        monkeypatch.delenv("PRIVATE_KEY_HEX", raising=False)
        kp = _import_kp()
        with pytest.raises(SystemExit):
            kp.EnvKeyProvider().load_wallets()

    def test_lock_is_noop(self, monkeypatch):
        kp = _import_kp()
        # Should not raise even with no setup.
        kp.EnvKeyProvider().lock()


# ---------------------------------------------------------------------------
# FileKeyProvider
# ---------------------------------------------------------------------------


class TestFileKeyProvider:
    def test_loads_chmod_600_file(self, tmp_path, monkeypatch):
        key_file = tmp_path / "treasury.key"
        key_file.write_text(TEST_HEX)
        os.chmod(key_file, 0o600)
        monkeypatch.setenv("PRIVATE_KEY_FILE", str(key_file))
        kp = _import_kp()
        provider = kp.FileKeyProvider()
        key = provider.load_wallets()[0].raw_key
        assert bytes(key) == TEST_BYTES

    @pytest.mark.invariant
    def test_world_readable_file_rejected(self, tmp_path, monkeypatch):
        """INVARIANT: a key file with mode 0644 must be refused at boot.
        If we ever soft-fail here, a casual `ls` from another user is
        enough to dump the treasury key."""
        key_file = tmp_path / "treasury.key"
        key_file.write_text(TEST_HEX)
        os.chmod(key_file, 0o644)  # group + other can read
        monkeypatch.setenv("PRIVATE_KEY_FILE", str(key_file))
        kp = _import_kp()
        with pytest.raises(SystemExit):
            kp.FileKeyProvider().load_wallets()

    @pytest.mark.invariant
    def test_group_readable_file_rejected(self, tmp_path, monkeypatch):
        """INVARIANT: a key file with mode 0640 (group read) must be
        refused at boot — even shared-group access is too loose."""
        key_file = tmp_path / "treasury.key"
        key_file.write_text(TEST_HEX)
        os.chmod(key_file, 0o640)  # group can read
        monkeypatch.setenv("PRIVATE_KEY_FILE", str(key_file))
        kp = _import_kp()
        with pytest.raises(SystemExit):
            kp.FileKeyProvider().load_wallets()

    def test_missing_file_exits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PRIVATE_KEY_FILE", str(tmp_path / "does_not_exist"))
        kp = _import_kp()
        with pytest.raises(SystemExit):
            kp.FileKeyProvider().load_wallets()

    def test_unset_path_exits(self, monkeypatch):
        monkeypatch.setenv("PRIVATE_KEY_FILE", "")
        kp = _import_kp()
        with pytest.raises(SystemExit):
            kp.FileKeyProvider().load_wallets()


# ---------------------------------------------------------------------------
# OnePasswordKeyProvider
# ---------------------------------------------------------------------------


class TestOnePasswordKeyProvider:
    def test_reads_from_op_cli(self, monkeypatch):
        kp = _import_kp()
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = TEST_HEX.encode() + b"\n"
        fake.stderr = b""
        with patch("skr_crypto.server.key_providers.subprocess.run", return_value=fake):
            wallets = kp.OnePasswordKeyProvider().load_wallets()
        assert len(wallets) == 1
        assert bytes(wallets[0].raw_key) == TEST_BYTES

    def test_op_not_found_exits(self):
        kp = _import_kp()
        with patch("skr_crypto.server.key_providers.subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(SystemExit):
                kp.OnePasswordKeyProvider().load_wallets()

    def test_op_nonzero_exits(self):
        kp = _import_kp()
        fake = MagicMock()
        fake.returncode = 1
        fake.stdout = b""
        fake.stderr = b"not signed in"
        with patch("skr_crypto.server.key_providers.subprocess.run", return_value=fake):
            with pytest.raises(SystemExit):
                kp.OnePasswordKeyProvider().load_wallets()


# ---------------------------------------------------------------------------
# KeychainKeyProvider
# ---------------------------------------------------------------------------


class TestKeychainKeyProvider:
    def test_reads_from_keychain_on_darwin(self, monkeypatch):
        kp = _import_kp()
        monkeypatch.setattr(sys, "platform", "darwin")
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = TEST_HEX.encode() + b"\n"
        fake.stderr = b""
        with patch("skr_crypto.server.key_providers.subprocess.run", return_value=fake):
            wallets = kp.KeychainKeyProvider().load_wallets()
        assert len(wallets) == 1
        assert bytes(wallets[0].raw_key) == TEST_BYTES

    def test_non_darwin_exits(self, monkeypatch):
        kp = _import_kp()
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(SystemExit):
            kp.KeychainKeyProvider().load_wallets()

    def test_security_returns_error_exits(self, monkeypatch):
        kp = _import_kp()
        monkeypatch.setattr(sys, "platform", "darwin")
        fake = MagicMock()
        fake.returncode = 44
        fake.stdout = b""
        fake.stderr = b"The specified item could not be found in the keychain."
        with patch("skr_crypto.server.key_providers.subprocess.run", return_value=fake):
            with pytest.raises(SystemExit):
                kp.KeychainKeyProvider().load_wallets()


# ---------------------------------------------------------------------------
# get_key_provider — selection
# ---------------------------------------------------------------------------


class TestGetKeyProvider:
    def test_each_known_value_returns_correct_class(self, monkeypatch):
        for name in ("1password", "env", "file", "keychain", "encrypted_file"):
            monkeypatch.setenv("KEY_PROVIDER", name)
            import skr_crypto.server.config as cfg
            importlib.reload(cfg)
            kp = _import_kp()
            inst = kp.get_key_provider()
            assert type(inst) is kp._REGISTRY[name]

    def test_unknown_provider_exits(self, monkeypatch):
        monkeypatch.setenv("KEY_PROVIDER", "bogus")
        import skr_crypto.server.config as cfg
        importlib.reload(cfg)
        kp = _import_kp()
        with pytest.raises(SystemExit):
            kp.get_key_provider()

    def test_supported_providers_lists_all(self, monkeypatch):
        kp = _import_kp()
        # 1.4.0 added encrypted_file as the fifth backend.
        assert set(kp.supported_providers()) == {
            "1password", "env", "file", "keychain", "encrypted_file",
        }
