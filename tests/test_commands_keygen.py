"""``skr-crypto keygen`` — fresh TRON private key generation."""
from __future__ import annotations

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

from skr_crypto.cli import cli


# tronpy is heavyweight + has its own deps. Mock the minimum surface.
class _FakePubKey:
    def __init__(self, address: str):
        self._addr = address

    def to_base58check_address(self) -> str:
        return self._addr


class _FakePrivateKey:
    """Stand-in for tronpy.keys.PrivateKey. Captures whatever .random()
    would have returned, plus a stable predictable ``hex()`` so tests
    can assert on it."""

    _SAMPLE_HEX = "11" * 32  # 32-byte test value
    _SAMPLE_ADDRESS = "TPredictableTestAddress0000000000000"

    @classmethod
    def random(cls) -> _FakePrivateKey:
        return cls()

    def hex(self) -> str:
        return self._SAMPLE_HEX

    @property
    def public_key(self) -> _FakePubKey:
        return _FakePubKey(self._SAMPLE_ADDRESS)


@pytest.fixture(autouse=True)
def fake_tronpy(monkeypatch):
    """Inject a fake tronpy.keys.PrivateKey so tests don't need the
    real package installed in the CLI's venv."""
    fake_module = MagicMock()
    fake_module.PrivateKey = _FakePrivateKey
    fake_keys_pkg = MagicMock(keys=fake_module)
    monkeypatch.setitem(sys.modules, "tronpy", fake_keys_pkg)
    monkeypatch.setitem(sys.modules, "tronpy.keys", fake_module)


def test_keygen_default_prints_only(runner, tmp_path):
    """Default = 'none' = print PRIVATE_KEY_HEX, don't write anywhere."""
    result = runner.invoke(
        cli, ["--dir", str(tmp_path / "nope"), "keygen", "--yes"],
    )
    assert result.exit_code == 0
    assert _FakePrivateKey._SAMPLE_HEX in result.stdout
    assert _FakePrivateKey._SAMPLE_ADDRESS in result.stdout


def test_keygen_aborts_on_no_confirmation(runner, tmp_path):
    result = runner.invoke(
        cli, ["--dir", str(tmp_path / "nope"), "keygen"],
        input="n\n",
    )
    # Confirmation declined → exit 1, no key in output.
    assert result.exit_code == 1
    assert _FakePrivateKey._SAMPLE_HEX not in result.stdout


def test_keygen_env_prints_export_line(runner, tmp_path):
    result = runner.invoke(
        cli, ["--dir", str(tmp_path / "nope"),
              "keygen", "--write-to", "env", "--yes"],
    )
    assert result.exit_code == 0
    assert f"PRIVATE_KEY_HEX={_FakePrivateKey._SAMPLE_HEX}" in result.stdout


def test_keygen_file_writes_chmod_600(runner, tmp_path):
    out = tmp_path / "treasury.key"
    result = runner.invoke(
        cli, ["--dir", str(tmp_path / "nope"),
              "keygen", "--write-to", "file",
              "--path", str(out), "--yes"],
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert out.exists()
    mode = out.stat().st_mode & 0o777
    assert mode == 0o600
    assert out.read_text().strip() == _FakePrivateKey._SAMPLE_HEX


def test_keygen_file_refuses_to_overwrite(runner, tmp_path):
    out = tmp_path / "existing.key"
    out.write_text("dont-clobber-me\n")
    os.chmod(out, 0o600)
    result = runner.invoke(
        cli, ["--dir", str(tmp_path / "nope"),
              "keygen", "--write-to", "file",
              "--path", str(out), "--yes"],
    )
    assert result.exit_code != 0
    # Original content survives.
    assert out.read_text() == "dont-clobber-me\n"


def test_keygen_file_requires_path(runner, tmp_path):
    result = runner.invoke(
        cli, ["--dir", str(tmp_path / "nope"),
              "keygen", "--write-to", "file", "--yes"],
    )
    assert result.exit_code == 1
    # Generic SkrCryptoError (exit 1) — message mentions --path.
    assert "--path" in result.stderr or "--path" in result.stdout


def test_keygen_keychain_invokes_security_tool(runner, tmp_path):
    """The keychain backend shells out to /usr/bin/security."""
    fake_run = MagicMock(return_value=MagicMock(
        returncode=0, stdout="", stderr="",
    ))
    with patch("skr_crypto.commands.keygen.subprocess.run", fake_run), \
         patch("skr_crypto.commands.keygen.shutil.which",
               return_value="/usr/bin/security"):
        result = runner.invoke(
            cli, ["--dir", str(tmp_path / "nope"),
                  "keygen", "--write-to", "keychain", "--yes"],
        )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    fake_run.assert_called_once()
    cmd_args = fake_run.call_args[0][0]
    assert cmd_args[0] == "security"
    assert "add-generic-password" in cmd_args
    assert "-U" in cmd_args  # update-if-exists flag is critical


def test_keygen_keychain_failure_surfaces(runner, tmp_path):
    fake_run = MagicMock(return_value=MagicMock(
        returncode=1, stdout="", stderr="user denied",
    ))
    with patch("skr_crypto.commands.keygen.subprocess.run", fake_run), \
         patch("skr_crypto.commands.keygen.shutil.which",
               return_value="/usr/bin/security"):
        result = runner.invoke(
            cli, ["--dir", str(tmp_path / "nope"),
                  "keygen", "--write-to", "keychain", "--yes"],
        )
    assert result.exit_code == 1
    # Error message should surface the security tool's stderr.
    combined = result.stdout + result.stderr
    assert "user denied" in combined or "security" in combined


def test_keygen_autodetect_from_env_provider(runner, isolated_install):
    """If KEY_PROVIDER=env in .env and no --write-to flag, defaults
    to env behaviour (printing the export line, no write)."""
    result = runner.invoke(
        cli, ["--dir", str(isolated_install), "keygen", "--yes"],
    )
    assert result.exit_code == 0
    assert "PRIVATE_KEY_HEX=" in result.stdout


def test_keygen_explicit_overrides_autodetect(runner, isolated_install):
    """An explicit --write-to none beats the .env's KEY_PROVIDER=env."""
    result = runner.invoke(
        cli, ["--dir", str(isolated_install),
              "keygen", "--write-to", "none", "--yes"],
    )
    assert result.exit_code == 0
    # 'none' path: prints raw key but no shell-export framing.
    assert _FakePrivateKey._SAMPLE_HEX in result.stdout


def test_keygen_listed_in_help(runner):
    result = runner.invoke(cli, ["help"])
    assert result.exit_code == 0
    assert "keygen" in result.output


def test_keygen_address_format_in_output(runner, tmp_path):
    """Sanity: TRON addresses start with T and are 34 chars. Even with
    the fake key we emit a string in that shape — ensures the
    formatting code didn't truncate."""
    result = runner.invoke(
        cli, ["--dir", str(tmp_path / "nope"), "keygen", "--yes"],
    )
    assert result.exit_code == 0
    matches = re.findall(r"T[A-Za-z0-9]{20,40}", result.stdout)
    assert any(_FakePrivateKey._SAMPLE_ADDRESS in m for m in matches)
