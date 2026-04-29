from __future__ import annotations

from skr_crypto.server.security import wipe_bytearray


class TestWipeBytearray:
    def test_wipe_zeros_out(self):
        buf = bytearray(b"\xab\xcd\xef\x01")
        wipe_bytearray(buf)
        assert buf == bytearray(b"\x00\x00\x00\x00")

    def test_wipe_empty(self):
        buf = bytearray()
        wipe_bytearray(buf)
        assert buf == bytearray()


class TestApiKeyAuth:
    def test_missing_key_returns_401(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, client):
        resp = client.get("/api/v1/health", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_valid_key_passes(self, client, auth_headers):
        resp = client.get("/api/v1/health", headers=auth_headers)
        assert resp.status_code == 200

    def test_empty_key_returns_401(self, client):
        resp = client.get("/api/v1/health", headers={"X-API-Key": ""})
        assert resp.status_code == 401
