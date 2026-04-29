"""``skr-crypto config show / edit / validate``."""
from __future__ import annotations

from skr_crypto.cli.main import cli


def test_config_show_masks_secrets(runner, isolated_install):
    result = runner.invoke(cli, [
        "--dir", str(isolated_install),
        "config", "show",
    ])
    assert result.exit_code == 0
    # AUTH_TOKEN value (test-token-not-for-prod-use-...) must NOT appear in full.
    assert "test-token-not-for-prod-use-aaaaaaaaaaaa" not in result.stdout
    assert "TRONGRID_API_KEY" in result.stdout
    # The masked form is "test…" — first 4 chars + …
    assert "test…" in result.stdout


def test_config_show_unsafe_unmasks(runner, isolated_install):
    result = runner.invoke(cli, [
        "--dir", str(isolated_install),
        "config", "show", "--unsafe-show-secrets",
    ])
    assert result.exit_code == 0
    assert "test-token-not-for-prod-use-aaaaaaaaaaaa" in result.stdout


def test_config_show_when_uninstalled(runner, tmp_path):
    result = runner.invoke(cli, [
        "--dir", str(tmp_path / "nope"),
        "config", "show",
    ])
    assert result.exit_code == 2  # NotInstalledError
