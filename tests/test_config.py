"""skr_crypto.config — install dir resolution and .env parsing."""
from __future__ import annotations

from pathlib import Path

import pytest

from skr_crypto import config
from skr_crypto.exceptions import ConfigError, NotInstalledError


class TestInstallDirResolution:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("SKR_CRYPTO_HOME", raising=False)
        assert config.install_dir() == config.DEFAULT_INSTALL_DIR.resolve()

    def test_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SKR_CRYPTO_HOME", str(tmp_path))
        assert config.install_dir() == tmp_path.resolve()

    def test_explicit_override_beats_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SKR_CRYPTO_HOME", "/should/not/be/used")
        assert config.install_dir(tmp_path) == tmp_path.resolve()


class TestRequireInstalled:
    def test_raises_when_dir_missing(self, tmp_path):
        with pytest.raises(NotInstalledError):
            config.require_installed(tmp_path / "nope")

    def test_raises_when_env_missing(self, tmp_path):
        # Has app/ but no .env
        (tmp_path / "app").mkdir()
        with pytest.raises(NotInstalledError):
            config.require_installed(tmp_path)

    def test_raises_when_app_missing(self, tmp_path):
        (tmp_path / ".env").write_text("AUTH_TOKEN=x\n")
        with pytest.raises(NotInstalledError):
            config.require_installed(tmp_path)

    def test_passes_when_both_present(self, isolated_install):
        assert config.require_installed(isolated_install) == isolated_install


class TestReadEnvFile:
    def test_basic(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("FOO=bar\nBAZ=qux\n")
        assert config.read_env_file(p) == {"FOO": "bar", "BAZ": "qux"}

    def test_strips_quotes(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text('FOO="quoted value"\nBAR=\'single\'\n')
        result = config.read_env_file(p)
        assert result["FOO"] == "quoted value"
        assert result["BAR"] == "single"

    def test_skips_comments_and_blanks(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("# comment\n\nKEY=value\n   # indented\n")
        assert config.read_env_file(p) == {"KEY": "value"}

    def test_strips_inline_comment(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("KEY=value # this is a comment\n")
        assert config.read_env_file(p) == {"KEY": "value"}

    def test_preserves_hash_in_value(self, tmp_path):
        """A # without a leading space is part of the value."""
        p = tmp_path / ".env"
        p.write_text("KEY=secret#token\n")
        assert config.read_env_file(p) == {"KEY": "secret#token"}

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ConfigError):
            config.read_env_file(tmp_path / "nope")


class TestWriteEnvFile:
    def test_writes_and_chmod_600(self, tmp_path):
        p = tmp_path / ".env"
        config.write_env_file(p, {"AUTH_TOKEN": "abc", "X": "y"})
        assert p.exists()
        # Mode is 0600 — sensitive data in this file
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600
        content = p.read_text()
        assert "AUTH_TOKEN=abc" in content
        assert "X=y" in content

    def test_skips_empty_values(self, tmp_path):
        p = tmp_path / ".env"
        config.write_env_file(p, {"FILLED": "ok", "EMPTY": ""})
        content = p.read_text()
        assert "FILLED=ok" in content
        assert "EMPTY" not in content


class TestServicePaths:
    def test_relative_paths_resolve_to_install_dir(self, tmp_path):
        env = {
            "AUDIT_LOG_FILE": "data/audit.log",
            "IDEMPOTENCY_DB_PATH": "data/idempotency.db",
        }
        paths = config.ServicePaths.from_install_dir(tmp_path, env)
        assert paths.audit_log == tmp_path / "data/audit.log"
        assert paths.idempotency_db == tmp_path / "data/idempotency.db"
        assert paths.data_dir == tmp_path / "data"
        assert paths.env_file == tmp_path / ".env"

    def test_absolute_paths_stay_absolute(self, tmp_path):
        env = {
            "AUDIT_LOG_FILE": "/var/log/payouts/audit.log",
            "IDEMPOTENCY_DB_PATH": "/var/lib/payouts/db.sqlite",
        }
        paths = config.ServicePaths.from_install_dir(tmp_path, env)
        assert paths.audit_log == Path("/var/log/payouts/audit.log")
        assert paths.idempotency_db == Path("/var/lib/payouts/db.sqlite")


class TestSensitiveKeys:
    def test_audit_token_is_sensitive(self):
        assert "AUTH_TOKEN" in config.SENSITIVE_KEYS

    def test_trongrid_key_is_sensitive(self):
        assert "TRONGRID_API_KEY" in config.SENSITIVE_KEYS

    def test_private_key_hex_is_sensitive(self):
        assert "PRIVATE_KEY_HEX" in config.SENSITIVE_KEYS
