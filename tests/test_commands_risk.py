"""``skr-crypto risk <addr>`` CLI tests."""
from __future__ import annotations

import json

import responses

from skr_crypto.cli.main import cli

VALID = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"


def _ok_report(level="low"):
    return {
        "address": VALID,
        "level": level,
        "checks": [
            {"name": "validity", "status": "ok", "message": "ok", "severity": "info"},
            {"name": "usdt_blacklist", "status": "ok", "message": "ok", "severity": "info"},
        ],
        "summary": {
            "trx_balance": "1.0", "usdt_balance": "100", "warmth": "warm",
        },
    }


@responses.activate
def test_risk_low_exits_0(runner, isolated_install, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/api/v1/risk/{VALID}?external=false",
        json=_ok_report("low"), status=200,
        match_querystring=True,
    )
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "risk", VALID,
    ])
    assert result.exit_code == 0, (result.stdout, result.stderr)
    # Verdict line present.
    assert "LOW" in result.stdout


@responses.activate
def test_risk_medium_exits_10(runner, isolated_install, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/api/v1/risk/{VALID}?external=false",
        json=_ok_report("medium"), status=200,
        match_querystring=True,
    )
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "risk", VALID,
    ])
    assert result.exit_code == 10


@responses.activate
def test_risk_high_exits_11(runner, isolated_install, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/api/v1/risk/{VALID}?external=false",
        json=_ok_report("high"), status=200,
        match_querystring=True,
    )
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "risk", VALID,
    ])
    assert result.exit_code == 11


@responses.activate
def test_risk_invalid_exits_12(runner, isolated_install, base_url):
    responses.add(
        responses.GET,
        f"{base_url}/api/v1/risk/garbage?external=false",
        json={"address": "garbage", "level": "invalid", "checks": [],
              "summary": {}},
        status=200,
        match_querystring=True,
    )
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "risk", "garbage",
    ])
    assert result.exit_code == 12


@responses.activate
def test_risk_external_flag_forwarded(runner, isolated_install, base_url):
    captured = []

    def callback(request):
        captured.append(request.url)
        return (200, {}, json.dumps(_ok_report("low")))

    responses.add_callback(
        responses.GET,
        f"{base_url}/api/v1/risk/{VALID}",
        callback=callback,
    )
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "risk", VALID, "--external",
    ])
    assert result.exit_code == 0
    assert "external=true" in captured[0]


@responses.activate
def test_risk_json_mode(runner, isolated_install, base_url):
    body = _ok_report("low")
    responses.add(
        responses.GET,
        f"{base_url}/api/v1/risk/{VALID}?external=false",
        json=body, status=200,
        match_querystring=True,
    )
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "risk", VALID, "--json",
    ])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["address"] == VALID
    assert payload["level"] == "low"


def test_risk_when_service_down(runner, isolated_install):
    """No mocked HTTP — connection refused → exit 4."""
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "risk", VALID,
    ])
    assert result.exit_code == 4


def test_risk_listed_in_help(runner):
    result = runner.invoke(cli, ["help"])
    assert result.exit_code == 0
    assert "risk" in result.output


def test_risk_help_mentions_external(runner):
    result = runner.invoke(cli, ["risk", "--help"])
    assert result.exit_code == 0
    assert "--external" in result.output
    assert "TronScan" in result.output
