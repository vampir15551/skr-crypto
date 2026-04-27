"""Top-level CLI behaviour: --version, help, command discovery."""
from __future__ import annotations

from skr_crypto.cli import cli
from skr_crypto.version import __version__


def test_cli_version_flag(runner):
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_cli_help_lists_all_groups(runner):
    """`skr-crypto help` must list at least these top-level commands."""
    result = runner.invoke(cli, ["help"])
    assert result.exit_code == 0
    for cmd in ("install", "update", "status", "logs", "balance",
                "check", "audit", "reconcile", "config",
                "backup", "restore", "doctor", "version", "help"):
        assert cmd in result.output, f"{cmd} missing from help output"


def test_help_topic_dispatches(runner):
    """`skr-crypto help install` should print install's --help."""
    result = runner.invoke(cli, ["help", "install"])
    assert result.exit_code == 0
    assert "--git-url" in result.output


def test_help_unknown_topic_errors(runner):
    result = runner.invoke(cli, ["help", "no-such-command"])
    assert result.exit_code != 0


def test_top_level_help_works(runner):
    """`skr-crypto --help` is click's default; should still work."""
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "install" in result.output
    assert "audit" in result.output


def test_no_command_shows_help(runner):
    """Running with no args shows help (click default for groups)."""
    result = runner.invoke(cli, [])
    # Click returns 0 (or 2 depending on version) and prints usage either way.
    assert "install" in result.output or "Usage" in result.output
