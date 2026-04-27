"""skr_crypto.api — HTTP client behaviour, error mapping."""
from __future__ import annotations

import pytest
import responses

from skr_crypto.api import APIClient
from skr_crypto.exceptions import (
    AuthError,
    BadResponseError,
    ConfigError,
    ServiceUnreachableError,
)


@pytest.fixture
def client(base_url):
    return APIClient(base_url, "test-token", timeout=2.0)


class TestFromEnvFile:
    def test_loads_token_and_constructs_url(self, isolated_install):
        client = APIClient.from_env_file(isolated_install / ".env")
        assert client.base_url == "http://127.0.0.1:8765"
        assert client._auth_token == "test-token-not-for-prod-use-aaaaaaaaaaaa"

    def test_missing_env_raises(self, tmp_path):
        with pytest.raises(ConfigError):
            APIClient.from_env_file(tmp_path / "nope.env")

    def test_zero_dot_zero_normalised_to_localhost(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("AUTH_TOKEN=t\nSERVER_HOST=0.0.0.0\nSERVER_PORT=9999\n")
        client = APIClient.from_env_file(env)
        # Listening on 0.0.0.0 means we hit it on 127.0.0.1 from the same host.
        assert client.base_url == "http://127.0.0.1:9999"


class TestEndpoints:
    @responses.activate
    def test_health_live_does_not_send_auth(self, client, base_url):
        rsp = responses.add(
            responses.GET, f"{base_url}/api/v1/health/live",
            json={"status": "ok", "uptime_seconds": 12},
            status=200,
        )
        result = client.health_live()
        assert result == {"status": "ok", "uptime_seconds": 12}
        # X-API-Key must NOT be sent for unauthenticated endpoints.
        assert "X-API-Key" not in rsp.calls[0].request.headers

    @responses.activate
    def test_balance_sends_auth(self, client, base_url):
        rsp = responses.add(
            responses.GET, f"{base_url}/api/v1/balance",
            json={"address": "T...", "trx": "100", "usdt": "5000"},
            status=200,
        )
        result = client.balance()
        assert result["address"] == "T..."
        assert rsp.calls[0].request.headers["X-API-Key"] == "test-token"

    @responses.activate
    def test_health_full_sends_auth(self, client, base_url):
        rsp = responses.add(
            responses.GET, f"{base_url}/api/v1/health",
            json={"status": "ok", "node_connected": True}, status=200,
        )
        client.health()
        assert rsp.calls[0].request.headers["X-API-Key"] == "test-token"

    @responses.activate
    def test_version_no_auth(self, client, base_url):
        rsp = responses.add(
            responses.GET, f"{base_url}/api/v1/version",
            json={"git_sha": "abc", "started_at": 1.0, "network": "nile"},
        )
        client.version()
        assert "X-API-Key" not in rsp.calls[0].request.headers


class TestErrorMapping:
    @responses.activate
    def test_401_becomes_auth_error(self, client, base_url):
        responses.add(
            responses.GET, f"{base_url}/api/v1/balance",
            json={"detail": "Invalid X-API-Key"}, status=401,
        )
        with pytest.raises(AuthError):
            client.balance()

    @responses.activate
    def test_500_becomes_bad_response(self, client, base_url):
        responses.add(
            responses.GET, f"{base_url}/api/v1/balance",
            body="oops", status=500,
        )
        with pytest.raises(BadResponseError):
            client.balance()

    @responses.activate
    def test_4xx_other_becomes_bad_response(self, client, base_url):
        responses.add(
            responses.GET, f"{base_url}/api/v1/balance",
            json={"error": "bad"}, status=400,
        )
        with pytest.raises(BadResponseError):
            client.balance()

    @responses.activate
    def test_non_json_body_becomes_bad_response(self, client, base_url):
        responses.add(
            responses.GET, f"{base_url}/api/v1/health/live",
            body="not json at all", status=200,
        )
        with pytest.raises(BadResponseError):
            client.health_live()

    def test_connection_refused_becomes_unreachable(self, base_url):
        # Pointing at a port that's almost certainly closed locally.
        client = APIClient("http://127.0.0.1:1", None, timeout=0.5)
        with pytest.raises(ServiceUnreachableError):
            client.health_live()


class TestMetricsEndpoint:
    @responses.activate
    def test_returns_text(self, client, base_url):
        prom_body = "# TYPE payouts_uptime_seconds gauge\npayouts_uptime_seconds 42\n"
        responses.add(
            responses.GET, f"{base_url}/api/v1/metrics",
            body=prom_body, status=200, content_type="text/plain",
        )
        text = client.metrics()
        assert "payouts_uptime_seconds 42" in text

    @responses.activate
    def test_401_raises(self, client, base_url):
        responses.add(
            responses.GET, f"{base_url}/api/v1/metrics",
            body="nope", status=401,
        )
        with pytest.raises(AuthError):
            client.metrics()
