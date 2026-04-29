"""
End-to-end tests against TRON Nile testnet.

These tests make REAL network calls — they are NOT run in normal CI.
Run manually:

    TRON_E2E=1 \
    E2E_PRIVATE_KEY=<hex-private-key> \
    E2E_TO_ADDRESS=<any-valid-nile-address> \
    python -m pytest tests/test_e2e_testnet.py -v -s

Prerequisites:
  - The wallet must have TRX on Nile (get from https://nileex.io/join/getJoinPage)
  - The wallet must have test USDT on Nile (USDT contract: TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf)
  - Use a DEDICATED test wallet — never your mainnet wallet

What these tests verify:
  - Real connection to TronGrid Nile node
  - Real balance queries (TRX + USDT)
  - Real USDT transfer (small amount) — broadcast and txid returned
  - Full round-trip through TronClient (no mocks)
"""
from __future__ import annotations

import os
import time
from decimal import Decimal

import pytest

# Skip entire module unless TRON_E2E=1 is set
pytestmark = pytest.mark.skipif(
    os.getenv("TRON_E2E") != "1",
    reason="Set TRON_E2E=1 to run testnet e2e tests",
)

NILE_USDT_CONTRACT = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"


@pytest.fixture(scope="module")
def e2e_client():
    """Create a real TronClient connected to Nile testnet."""
    from tronpy import Tron
    from tronpy.keys import PrivateKey
    from tronpy.providers import HTTPProvider

    from skr_crypto.server.tron_client import TronClient

    private_key_hex = os.environ.get("E2E_PRIVATE_KEY", "")
    if not private_key_hex:
        pytest.skip("E2E_PRIVATE_KEY not set")

    # Strip 0x prefix
    if private_key_hex.startswith(("0x", "0X")):
        private_key_hex = private_key_hex[2:]

    client = TronClient()
    provider = HTTPProvider("https://nile.trongrid.io")
    client.client = Tron(provider=provider)
    client.priv_key = PrivateKey(bytes.fromhex(private_key_hex))
    client.address = client.priv_key.public_key.to_base58check_address()

    # Override USDT contract to Nile's test token
    client._usdt_contract = client.client.get_contract(NILE_USDT_CONTRACT)

    print(f"\n  E2E wallet: {client.address}")
    print("  Network: nile")
    print(f"  USDT contract: {NILE_USDT_CONTRACT}")

    yield client

    client.destroy()


@pytest.fixture(scope="module")
def to_address():
    addr = os.environ.get("E2E_TO_ADDRESS", "")
    if not addr:
        pytest.skip("E2E_TO_ADDRESS not set")
    return addr


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestE2EConnection:
    def test_node_reachable(self, e2e_client):
        """Can we reach the Nile node?"""
        assert e2e_client.check_connection() is True

    def test_wallet_address_valid(self, e2e_client):
        assert e2e_client.address.startswith("T")
        assert len(e2e_client.address) == 34


class TestE2EBalances:
    def test_trx_balance(self, e2e_client):
        """Wallet should have some TRX for fees."""
        trx = e2e_client.get_trx_balance()
        print(f"  TRX balance: {trx}")
        assert isinstance(trx, Decimal)
        assert trx >= 0

    def test_usdt_balance(self, e2e_client):
        """Query USDT balance — even if 0, it should not error."""
        usdt = e2e_client.get_usdt_balance()
        print(f"  USDT balance: {usdt}")
        assert isinstance(usdt, Decimal)
        assert usdt >= 0


class TestE2ETransfer:
    def test_send_small_usdt(self, e2e_client, to_address):
        """Send 0.01 test USDT on Nile. Verifies full broadcast flow."""
        amount = Decimal("0.01")

        # Pre-check balance
        balance = e2e_client.get_usdt_balance()
        if balance < amount:
            pytest.skip(f"Insufficient test USDT: {balance} < {amount}")

        trx_balance = e2e_client.get_trx_balance()
        if trx_balance < Decimal("5"):
            pytest.skip(f"Insufficient TRX for fees: {trx_balance}")

        print(f"\n  Sending {amount} test USDT to {to_address}...")
        started = time.time()
        txid = e2e_client.send_usdt(to_address, amount)
        elapsed = time.time() - started

        print(f"  txid: {txid}")
        print(f"  elapsed: {elapsed:.2f}s")
        print(f"  explorer: https://nile.tronscan.org/#/transaction/{txid}")

        assert txid, "txid should not be empty"
        assert len(txid) == 64, f"txid should be 64 hex chars, got {len(txid)}"
