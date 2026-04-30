from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from skr_crypto.server.tron_client import MAX_RETRIES, TronClient, _is_transient_error, _with_retry


class TestWithRetry:
    def test_success_first_try(self):
        fn = MagicMock(return_value="ok")
        result = _with_retry(fn, "test")
        assert result == "ok"
        assert fn.call_count == 1

    @patch("skr_crypto.server.tron_client.time.sleep")
    def test_retry_on_429(self, mock_sleep):
        fn = MagicMock(side_effect=[Exception("429 Too Many Requests"), "ok"])
        result = _with_retry(fn, "test")
        assert result == "ok"
        assert fn.call_count == 2
        mock_sleep.assert_called_once()

    @patch("skr_crypto.server.tron_client.time.sleep")
    def test_retry_on_502(self, mock_sleep):
        fn = MagicMock(side_effect=[Exception("502 Bad Gateway"), "ok"])
        result = _with_retry(fn, "test")
        assert result == "ok"
        assert fn.call_count == 2

    @patch("skr_crypto.server.tron_client.time.sleep")
    def test_gives_up_after_max_retries(self, mock_sleep):
        fn = MagicMock(side_effect=Exception("503 Service Unavailable"))
        with pytest.raises(Exception, match="503"):
            _with_retry(fn, "test")
        assert fn.call_count == MAX_RETRIES

    def test_no_retry_on_business_error(self):
        fn = MagicMock(side_effect=ValueError("bad input"))
        with pytest.raises(ValueError, match="bad input"):
            _with_retry(fn, "test")
        assert fn.call_count == 1

    @patch("skr_crypto.server.tron_client.time.sleep")
    def test_backoff_increases(self, mock_sleep):
        fn = MagicMock(side_effect=[
            Exception("429"), Exception("429"), "ok",
        ])
        _with_retry(fn, "test")
        calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert calls[1] > calls[0]  # exponential backoff


class TestTronClientCheckConnection:
    def test_check_connection_true(self, mock_tron):
        mock_tron.client.get_latest_block_number = MagicMock(return_value=12345)
        assert TronClient.check_connection(mock_tron) is True

    def test_check_connection_false(self, mock_tron):
        mock_tron.client.get_latest_block_number = MagicMock(side_effect=Exception("timeout"))
        assert TronClient.check_connection(mock_tron) is False


class TestTransientErrorDetection:
    def test_429_is_transient(self):
        assert _is_transient_error(Exception("429 Too Many Requests")) is True

    def test_5xx_is_transient(self):
        for code in ("500", "502", "503", "504"):
            assert _is_transient_error(Exception(f"{code} oops")) is True

    def test_timeout_class_name_is_transient(self):
        class ReadTimeout(Exception):
            pass
        assert _is_transient_error(ReadTimeout("nope")) is True

    def test_timeout_message_is_transient(self):
        assert _is_transient_error(Exception("HTTPSConnectionPool: Read timed out")) is True

    def test_business_error_is_not_transient(self):
        assert _is_transient_error(ValueError("invalid amount")) is False


class TestSendUsdtBroadcastResult:
    """Critical: broadcast() returning anything other than an unambiguous
    success must raise — otherwise an empty txid would be cached as 'the
    txid' and all subsequent retries would return status=duplicate with
    no real on-chain transaction.

    1.4.0 moved signing from TronClient into Wallet, but the broadcast-
    response handling is identical. Tests now build a Wallet directly.
    """

    def _make_wallet_and_contract(self, broadcast_response):
        """Wallet + contract where ``contract.transfer().build().sign()
        .broadcast()`` returns ``broadcast_response``."""
        from skr_crypto.server.wallet import Wallet

        txn_mock = MagicMock()
        txn_mock.broadcast = MagicMock(return_value=broadcast_response)

        builder = MagicMock()
        builder.with_owner.return_value = builder
        builder.fee_limit.return_value = builder
        builder.build.return_value = builder
        builder.sign.return_value = txn_mock

        contract_mock = MagicMock()
        contract_mock.functions.transfer.return_value = builder

        wallet = Wallet(
            name="default",
            address="TTestAddress1234567890123456789012",
            priv_key=MagicMock(),
        )
        return wallet, contract_mock

    def test_success(self):
        wallet, contract = self._make_wallet_and_contract(
            {"result": True, "txid": "a" * 64},
        )
        txid = wallet.send_usdt(
            MagicMock(), contract,
            "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL", Decimal("1"),
        )
        assert txid == "a" * 64

    def test_empty_txid_raises(self):
        """Even if result=True, an empty txid is unsafe to cache."""
        wallet, contract = self._make_wallet_and_contract(
            {"result": True, "txid": ""},
        )
        with pytest.raises(RuntimeError, match="empty txid"):
            wallet.send_usdt(
                MagicMock(), contract,
                "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL", Decimal("1"),
            )

    def test_missing_txid_raises(self):
        wallet, contract = self._make_wallet_and_contract({"result": True})
        with pytest.raises(RuntimeError, match="empty txid"):
            wallet.send_usdt(
                MagicMock(), contract,
                "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL", Decimal("1"),
            )

    def test_node_rejection_raises(self):
        wallet, contract = self._make_wallet_and_contract(
            {"code": "BANDWIDTH_ERROR", "message": "out of bandwidth"},
        )
        with pytest.raises(RuntimeError, match="broadcast rejected"):
            wallet.send_usdt(
                MagicMock(), contract,
                "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL", Decimal("1"),
            )

    def test_result_false_raises_even_with_txid(self):
        """Some failure modes return both result=False and a txid; reject anyway."""
        wallet, contract = self._make_wallet_and_contract(
            {"result": False, "code": "SIGERROR", "txid": "deadbeef"},
        )
        with pytest.raises(RuntimeError, match="broadcast rejected"):
            wallet.send_usdt(
                MagicMock(), contract,
                "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL", Decimal("1"),
            )

    def test_unexpected_response_type_raises(self):
        wallet, contract = self._make_wallet_and_contract("not a dict")
        with pytest.raises(RuntimeError, match="unexpected broadcast response type"):
            wallet.send_usdt(
                MagicMock(), contract,
                "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL", Decimal("1"),
            )


class TestEnergyPriceTTL:
    """Energy price has a TTL so a chain proposal that changes EnergyFee
    is picked up without restarting the service. Within the TTL the value
    is reused; on RPC failure after a successful fetch we keep the cached
    value rather than falling back to the configured constant."""

    def _client_with_chain_params(self, params_responses):
        """Build a TronClient whose get_chain_parameters cycles through
        `params_responses` (one per call). An exception in the list is
        raised when its turn comes. The closure also stashes a `calls`
        list on the client so tests can assert how many real fetches
        happened."""
        client = TronClient()
        client.client = MagicMock()

        responses = list(params_responses)
        calls: list[int] = []

        def fake_get_params():
            calls.append(1)
            r = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(r, Exception):
                raise r
            return r

        client.client.get_chain_parameters = fake_get_params
        client._test_calls = calls  # type: ignore[attr-defined]
        return client

    def test_first_call_fetches_and_caches(self):
        client = self._client_with_chain_params(
            [[{"key": "getEnergyFee", "value": 280}]]
        )
        assert client.energy_price_sun() == 280
        # Second call within TTL must not re-fetch.
        assert client.energy_price_sun() == 280
        assert len(client._test_calls) == 1

    def test_ttl_expiry_refetches(self, monkeypatch):
        from skr_crypto.server import tron_client as tc
        client = self._client_with_chain_params([
            [{"key": "getEnergyFee", "value": 280}],
            [{"key": "getEnergyFee", "value": 100}],
        ])
        # First fetch
        assert client.energy_price_sun() == 280
        # Force expiry by rewinding the cache timestamp past the TTL
        client._energy_price_fetched_at = time.time() - tc._ENERGY_PRICE_TTL_SEC - 1
        # Now should re-fetch and pick up the new chain price
        assert client.energy_price_sun() == 100

    def test_rpc_failure_keeps_cached_value(self, monkeypatch):
        from skr_crypto.server import tron_client as tc
        # Use a non-transient exception so _with_retry doesn't sleep through
        # MAX_RETRIES — we're testing TTL semantics, not retry behaviour.
        client = self._client_with_chain_params([
            [{"key": "getEnergyFee", "value": 280}],
            ValueError("chain parameters unavailable"),
        ])
        assert client.energy_price_sun() == 280
        client._energy_price_fetched_at = time.time() - tc._ENERGY_PRICE_TTL_SEC - 1
        # Refresh fails; the previously-cached real value must survive.
        assert client.energy_price_sun() == 280

    def test_initial_failure_uses_fallback(self):
        from skr_crypto.server.config import TRON_ENERGY_PRICE_SUN_FALLBACK
        client = self._client_with_chain_params(
            [ValueError("chain parameters unavailable")]
        )
        # No prior cached value → must use the configured fallback.
        assert client.energy_price_sun() == TRON_ENERGY_PRICE_SUN_FALLBACK
