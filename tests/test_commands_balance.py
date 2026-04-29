"""``skr-crypto balance`` — wraps the /api/v1/balance endpoint."""
from __future__ import annotations

import json

import responses

from skr_crypto.cli.main import cli


@responses.activate
def test_balance_ok_human(runner, isolated_install, base_url):
    responses.add(
        responses.GET, f"{base_url}/api/v1/balance",
        json={
            "address": "TXpdZ6qvm693uDL8iVnXGQjjPt7oh6MA2T",
            "trx": "100.5",
            "usdt": "5000.123456",
            "energy_available": 25000,
            "bandwidth_free_available": 600,
            "bandwidth_paid_available": 0,
            "tron_power_staked": 0,
        }, status=200,
    )
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "balance",
    ])
    assert result.exit_code == 0
    assert "100.5" in result.stdout
    assert "5000.123456" in result.stdout
    assert "TXpdZ6qvm693uDL8iVnXGQjjPt7oh6MA2T" in result.stdout


@responses.activate
def test_balance_json(runner, isolated_install, base_url):
    body = {"address": "T...", "trx": "1", "usdt": "2"}
    responses.add(responses.GET, f"{base_url}/api/v1/balance",
                  json=body, status=200)
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "balance", "--json",
    ])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert parsed["address"] == "T..."


@responses.activate
def test_balance_401_maps_to_auth_error(runner, isolated_install, base_url):
    responses.add(
        responses.GET, f"{base_url}/api/v1/balance",
        json={"detail": "Invalid X-API-Key"}, status=401,
    )
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "balance",
    ])
    # AuthError → exit code 5
    assert result.exit_code == 5


def test_balance_when_service_down(runner, isolated_install):
    """No mock → connection refused → ServiceUnreachableError → exit 4."""
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "balance",
    ])
    assert result.exit_code == 4
