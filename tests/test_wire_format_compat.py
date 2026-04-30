"""Wire-format compatibility tests.

Each `tests/fixtures/api_v1_golden/v_X_Y_required_fields.json` is the
list of fields a client of that version expects on each response.
Adding fields is fine — removing or renaming them is a BREAKING
change that requires a major version bump.

When the wire format evolves:

  - Add the new fields to the most recent golden file. Don't modify
    older goldens — they record what was promised then.
  - If you remove or rename a field, BREAKING entry in CHANGELOG +
    major version bump + ADR.
  - If `pytest tests/test_wire_format_compat.py` fails, you broke
    a v1.X client. Fix the response, not the test.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skr_crypto.server.wallet import Wallet
from skr_crypto.server.wallet_pool import wallets as pool

pytestmark = [pytest.mark.invariant]


GOLDEN_DIR = Path(__file__).parent / "fixtures" / "api_v1_golden"
VALID_TO = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"


def _golden(version: str) -> dict:
    """Load a golden file by version label (e.g. 'v1_0', 'v1_4')."""
    path = GOLDEN_DIR / f"{version}_required_fields.json"
    return json.loads(path.read_text())


def _check(payload: dict, required: list[str], context: str) -> None:
    missing = [f for f in required if f not in payload]
    assert not missing, (
        f"BREAKING wire-format change in {context}: missing fields "
        f"{missing}. If this is intentional, bump major version + add "
        f"a 'BREAKING:' entry in CHANGELOG. If not, restore the fields."
    )


# ---------------------------------------------------------------------------
# v1.0 contract — every existing caller pinned at v1.0 must still parse
# ---------------------------------------------------------------------------


class TestV1_0Compat:
    """v1.0 callers expect these fields. Removing any is breaking."""

    def test_send_success_includes_v1_0_required_fields(self, client, auth_headers, mock_tron):
        resp = client.post("/api/v1/send", headers=auth_headers, json={
            "to_address": VALID_TO,
            "amount": "1.0",
            "idempotency_key": "compat-v10-success",
        })
        assert resp.status_code == 200, resp.text
        required = _golden("v1_0")["responses"]["POST /api/v1/send (success)"]
        _check(resp.json(), required, "POST /send (success)")

    def test_send_duplicate_includes_v1_0_required_fields(self, client, auth_headers, mock_tron):
        body = {
            "to_address": VALID_TO, "amount": "1.0",
            "idempotency_key": "compat-v10-dup",
        }
        first = client.post("/api/v1/send", headers=auth_headers, json=body)
        assert first.status_code == 200
        second = client.post("/api/v1/send", headers=auth_headers, json=body)
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        required = _golden("v1_0")["responses"]["POST /api/v1/send (duplicate)"]
        _check(second.json(), required, "POST /send (duplicate)")

    def test_balance_includes_v1_0_required_fields(self, client, auth_headers, mock_tron):
        resp = client.get("/api/v1/balance", headers=auth_headers)
        assert resp.status_code == 200
        required = _golden("v1_0")["responses"]["GET /api/v1/balance"]
        _check(resp.json(), required, "GET /balance")

    def test_health_includes_v1_0_required_fields(self, client, auth_headers, mock_tron):
        resp = client.get("/api/v1/health", headers=auth_headers)
        assert resp.status_code == 200
        required = _golden("v1_0")["responses"]["GET /api/v1/health"]
        _check(resp.json(), required, "GET /health")

    def test_health_live_includes_v1_0_required_fields(self, client):
        resp = client.get("/api/v1/health/live")
        assert resp.status_code == 200
        required = _golden("v1_0")["responses"]["GET /api/v1/health/live"]
        _check(resp.json(), required, "GET /health/live")

    def test_version_includes_v1_0_required_fields(self, client):
        resp = client.get("/api/v1/version")
        assert resp.status_code == 200
        required = _golden("v1_0")["responses"]["GET /api/v1/version"]
        _check(resp.json(), required, "GET /version")

    def test_error_responses_include_error_and_code(self, client, auth_headers):
        """Every documented error class returns {error, code, ...}."""
        # Trigger an INVALID_ADDRESS — short address.
        resp = client.post("/api/v1/send", headers=auth_headers, json={
            "to_address": "T" + "x" * 33,  # 34 chars but invalid checksum
            "amount": "1.0",
            "idempotency_key": "compat-v10-err",
        })
        assert resp.status_code in (400, 500), resp.text
        required = _golden("v1_0")["error_responses"]["400/500/etc"]
        _check(resp.json(), required, "error response")


# ---------------------------------------------------------------------------
# v1.4 contract — multi-wallet additions
# ---------------------------------------------------------------------------


class TestV1_4Compat:
    """Fields added in 1.4.0. v1.4 clients expect these in addition
    to the v1.0 base set."""

    def test_send_includes_wallet_field(self, client, auth_headers, mock_tron):
        resp = client.post("/api/v1/send", headers=auth_headers, json={
            "to_address": VALID_TO,
            "amount": "1.0",
            "idempotency_key": "compat-v14-send",
        })
        assert resp.status_code == 200
        required = _golden("v1_4")["responses"]["POST /api/v1/send (success)"]
        _check(resp.json(), required, "POST /send v1.4 fields")

    def test_balance_includes_wallet_field(self, client, auth_headers, mock_tron):
        resp = client.get("/api/v1/balance", headers=auth_headers)
        assert resp.status_code == 200
        required = _golden("v1_4")["responses"]["GET /api/v1/balance"]
        _check(resp.json(), required, "GET /balance v1.4 fields")

    def test_wallets_endpoint_shape(self, client, auth_headers, mock_tron):
        resp = client.get("/api/v1/wallets", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        # Top-level fields
        required_top = _golden("v1_4")["responses"]["GET /api/v1/wallets"]
        _check(body, required_top, "GET /wallets top-level")
        # Per-wallet fields
        assert len(body["wallets"]) >= 1, "expected at least one wallet"
        required_inner = _golden("v1_4")["responses"]["GET /api/v1/wallets[].wallets[]"]
        for w in body["wallets"]:
            _check(w, required_inner, "GET /wallets[i]")

    def test_health_includes_wallet_count(self, client, auth_headers, mock_tron):
        resp = client.get("/api/v1/health", headers=auth_headers)
        assert resp.status_code == 200
        required = _golden("v1_4")["responses"]["GET /api/v1/health"]
        _check(resp.json(), required, "GET /health v1.4 fields")

    def test_wallet_not_found_error_shape(self, client, auth_headers, mock_tron):
        # Need a multi-wallet pool for this — overlay two
        a = Wallet(name="hot", address="THotxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", priv_key=MagicMock())
        b = Wallet(name="cold", address="TColdxxxxxxxxxxxxxxxxxxxxxxxxxxxx", priv_key=MagicMock())
        pool._override_for_tests([a, b])
        resp = client.post("/api/v1/send", headers=auth_headers, json={
            "to_address": VALID_TO,
            "amount": "1.0",
            "idempotency_key": "compat-v14-notfound",
            "wallet": "ghost",
        })
        assert resp.status_code == 404
        required = _golden("v1_4")["error_responses"]["WALLET_NOT_FOUND (404)"]
        _check(resp.json(), required, "WALLET_NOT_FOUND error")
