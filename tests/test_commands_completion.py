"""``skr-crypto completion <shell>`` — shell-completion script generator."""
from __future__ import annotations

from skr_crypto.cli.main import cli


def test_completion_help_lists_shells(runner):
    result = runner.invoke(cli, ["completion", "--help"])
    assert result.exit_code == 0
    for shell in ("bash", "zsh", "fish"):
        assert shell in result.output


def test_completion_invalid_shell(runner):
    result = runner.invoke(cli, ["completion", "elvish"])
    assert result.exit_code != 0
    assert "invalid value" in (result.stdout + result.stderr).lower()


def test_completion_listed_in_help(runner):
    result = runner.invoke(cli, ["help"])
    assert result.exit_code == 0
    assert "completion" in result.output


def test_completion_fails_clean_when_no_binary(runner, monkeypatch):
    """If no skr-crypto on PATH and argv[0] doesn't end with skr-crypto,
    completion must fail with a clear error, not crash."""
    import sys
    monkeypatch.setattr(sys, "argv", ["pytest"])

    import shutil
    monkeypatch.setattr(shutil, "which",
                        lambda cmd: None if cmd == "skr-crypto" else "/usr/bin/" + cmd)

    result = runner.invoke(cli, ["completion", "zsh"])
    assert result.exit_code == 1
    assert "skr-crypto" in (result.stdout + result.stderr)
