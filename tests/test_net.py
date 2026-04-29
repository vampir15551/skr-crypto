"""Tests for client IP resolution with TRUSTED_PROXIES / X-Forwarded-For."""
from __future__ import annotations

from unittest.mock import MagicMock

from skr_crypto.server import net


def _fake_request(host: str, xff: str | None = None):
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = host
    headers = {}
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    req.headers = headers
    return req


class TestClientAddress:
    def test_direct_peer_when_no_proxy_configured(self, monkeypatch):
        monkeypatch.setattr(net, "TRUSTED_PROXIES", ())
        req = _fake_request("1.2.3.4", xff="10.0.0.1")
        # Even with XFF set, if no trusted proxies — ignore it (anti-spoof).
        assert net.client_address(req) == "1.2.3.4"

    def test_xff_used_when_peer_is_trusted_proxy(self, monkeypatch):
        monkeypatch.setattr(net, "TRUSTED_PROXIES", ("10.0.0.1",))
        req = _fake_request("10.0.0.1", xff="203.0.113.9, 10.0.0.1")
        assert net.client_address(req) == "203.0.113.9"

    def test_xff_ignored_when_peer_not_trusted(self, monkeypatch):
        monkeypatch.setattr(net, "TRUSTED_PROXIES", ("10.0.0.1",))
        # Attacker from a different IP tries to spoof via XFF — we ignore.
        req = _fake_request("198.51.100.5", xff="evil-client")
        assert net.client_address(req) == "198.51.100.5"

    def test_xff_empty_falls_back_to_peer(self, monkeypatch):
        monkeypatch.setattr(net, "TRUSTED_PROXIES", ("10.0.0.1",))
        req = _fake_request("10.0.0.1", xff="")
        assert net.client_address(req) == "10.0.0.1"

    def test_xff_missing_header_falls_back_to_peer(self, monkeypatch):
        monkeypatch.setattr(net, "TRUSTED_PROXIES", ("10.0.0.1",))
        req = _fake_request("10.0.0.1", xff=None)
        assert net.client_address(req) == "10.0.0.1"

    def test_no_client_returns_unknown(self, monkeypatch):
        monkeypatch.setattr(net, "TRUSTED_PROXIES", ("10.0.0.1",))
        req = MagicMock()
        req.client = None
        req.headers = {"X-Forwarded-For": "should-be-ignored"}
        assert net.client_address(req) == "unknown"
