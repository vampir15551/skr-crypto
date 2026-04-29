"""``skr-crypto status`` happy + sad paths."""
from __future__ import annotations

import json

import responses

from skr_crypto.cli.main import cli


def test_status_when_not_installed(runner, tmp_path):
    """If the install dir is empty, status prints 'not installed'."""
    result = runner.invoke(cli, ["--dir", str(tmp_path / "empty"), "status"])
    assert result.exit_code == 0
    assert "not installed" in result.stdout


def test_status_json_when_not_installed(runner, tmp_path):
    result = runner.invoke(
        cli, ["--dir", str(tmp_path / "empty"), "status", "--json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {"installed": False}


@responses.activate
def test_status_running(runner, isolated_install, base_url):
    responses.add(
        responses.GET, f"{base_url}/api/v1/health/live",
        json={"status": "ok", "uptime_seconds": 42}, status=200,
    )
    responses.add(
        responses.GET, f"{base_url}/api/v1/version",
        json={"git_sha": "abcdef123456", "started_at": 1700000000.0,
              "network": "nile"}, status=200,
    )
    result = runner.invoke(
        cli, ["--dir", str(isolated_install), "status", "--json"],
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["installed"] is True
    assert payload["running"] is True
    assert payload["uptime_seconds"] == 42
    assert payload["service_sha"] == "abcdef123456"
    assert payload["network"] == "nile"


def test_status_not_running(runner, isolated_install):
    """No HTTP mock → connection refused → running=False."""
    result = runner.invoke(
        cli, ["--dir", str(isolated_install), "status", "--json"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["installed"] is True
    assert payload["running"] is False


def test_status_human_renders_table(runner, isolated_install):
    """Without --json, output is a Rich table — should contain 'install dir'."""
    result = runner.invoke(cli, ["--dir", str(isolated_install), "status"])
    assert result.exit_code == 0
    assert "install dir" in result.stdout
