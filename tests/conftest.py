"""Shared test fixtures.

Hosts both CLI-side and server-side fixtures because we run both test
suites with one pytest invocation. Two principles:

  1. Never touch the user's real install dir or .env. Every test that
     reads ``~/.skr-crypto`` is invoked with ``--dir tmp_path``. The
     ``isolated_install`` fixture builds a minimal valid install.
  2. No real network. ``responses`` mocks all HTTP from CLI side;
     server-side fixtures mock the TronClient singleton so no
     TronGrid call escapes. Tests that forget to mock will fail loudly
     (connection refused on a stranger socket).
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

# Neutralise dotenv before any server module import. ``server.config``
# calls ``load_dotenv()`` at module top, and several tests reload it.
# If a real .env at the project root exists with prod values, every
# reload would silently re-inject AUDIT_LOG_FILE / IDEMPOTENCY_DB_PATH
# and tests would drift toward production paths.
import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: True
dotenv.find_dotenv = lambda *args, **kwargs: ""

# Set env BEFORE any server module imports.
os.environ.setdefault("AUTH_TOKEN", "test-secret-token")
os.environ.setdefault("TRON_NETWORK", "nile")
os.environ.setdefault("TRONGRID_API_KEY", "")
os.environ.setdefault("USDT_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
os.environ.setdefault("SHUTDOWN_TIMEOUT", "600")
os.environ.setdefault("RATE_LIMIT_MAX", "100")
os.environ.setdefault("RATE_LIMIT_WINDOW", "60")

# Hard override: if the operator has these in .env for prod, do NOT
# inherit them. Tests would otherwise write into the production audit
# log or share the production SQLite idempotency DB (autouse fixture
# below would then wipe live data).
os.environ["AUDIT_LOG_FILE"] = ""
os.environ["IDEMPOTENCY_DB_PATH"] = ""

# RISK_USE_EXTERNAL defaults to true in production (1.3.0+) so
# /send hits TronScan + (if keyed) MistTrack. Tests that want to
# exercise external risk providers explicitly mock with `responses`;
# everything else should not leak real HTTP. Force off here so the
# default path stays hermetic.
os.environ["RISK_USE_EXTERNAL"] = "false"
os.environ["MISTTRACK_API_KEY"] = ""
# Sanctions list is fetched on lifespan startup; tests bypass the
# lifespan but the URL refresh in conftest's mock_tron fixture would
# still hit the network if SANCTIONS_LIST_REFRESH=true. Lock down.
os.environ["SANCTIONS_LIST_REFRESH"] = "false"

import pytest  # noqa: E402
from click.testing import CliRunner  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Now safe to import server modules.
import skr_crypto.server.config as cfg  # noqa: E402

cfg.AUTH_TOKEN = "test-secret-token"
cfg.TRON_NETWORK = "nile"


# ---------------------------------------------------------------------------
# CLI-side fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """Click's test runner. Click 8.2 dropped ``mix_stderr`` and split
    stderr by default — that's what we want, since tests assert on
    stdout (data) vs stderr (status) independently."""
    return CliRunner()


@pytest.fixture
def isolated_install(tmp_path: Path) -> Path:
    """Minimal-but-valid 'installed' service at tmp_path.

    Includes:
      - .env with safe defaults + a known AUTH_TOKEN
      - app/ stub so ``require_installed`` accepts it (legacy name kept
        for backwards-compat with the older clone-into-dir flow)
      - data/ dir
      - venv/bin/python placeholder so check / reconcile commands
        could in theory run (tests mock subprocess anyway).
    """
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "data").mkdir()
    (tmp_path / ".env").write_text(
        "AUTH_TOKEN=test-token-not-for-prod-use-aaaaaaaaaaaa\n"
        "TRON_NETWORK=nile\n"
        "TRONGRID_API_KEY=test-key\n"
        "USDT_CONTRACT=TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t\n"
        "KEY_PROVIDER=env\n"
        "AUDIT_LOG_FILE=data/audit.log\n"
        "IDEMPOTENCY_DB_PATH=data/idempotency.db\n"
        "SERVER_HOST=127.0.0.1\n"
        "SERVER_PORT=8765\n"
    )
    os.chmod(tmp_path / ".env", 0o600)

    (tmp_path / "venv" / "bin").mkdir(parents=True)
    fake_py = tmp_path / "venv" / "bin" / "python"
    fake_py.write_text("#!/bin/sh\necho FAKE\n")
    os.chmod(fake_py, 0o755)

    return tmp_path


@pytest.fixture
def base_url() -> str:
    """The URL the APIClient computes from the isolated_install env."""
    return "http://127.0.0.1:8765"


# ---------------------------------------------------------------------------
# Server-side fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_tron():
    """Patch the tron singleton with predictable return values.

    In 1.4.0 the singleton went keyless (the old ``tron.address`` /
    ``tron.priv_key`` / single-wallet methods are gone — see CHANGELOG).
    The fixture now mocks the address-parameterised RPC methods AND
    seeds the WalletPool with one wallet so existing /send tests
    work unchanged. Tests that need multiple wallets override the pool
    via ``wallets._override_for_tests([...])`` after the fixture runs.
    """
    from skr_crypto.server.tron_client import tron
    from skr_crypto.server.wallet import Wallet
    from skr_crypto.server.wallet_pool import wallets as pool

    original_init = tron.init
    original_destroy = tron.destroy

    tron.init = MagicMock()
    tron.destroy = MagicMock()
    tron.client = MagicMock()
    tron._usdt_contract = MagicMock()

    # Address-parameterised reads — return the same balances regardless
    # of which wallet address is passed. Tests that need divergent
    # per-address balances override these with side_effect.
    tron.get_trx_balance_for = MagicMock(return_value=Decimal("100"))
    tron.get_usdt_balance_for = MagicMock(return_value=Decimal("5000"))
    tron.check_connection = MagicMock(return_value=True)

    tron.estimate_transfer_energy = MagicMock(return_value=None)
    tron.energy_price_sun = MagicMock(return_value=420)
    tron.compute_fee_limit_sun = MagicMock(return_value=30_000_000)
    tron.get_resource_summary_for = MagicMock(return_value={
        "energy_available": 0,
        "energy_limit": 0,
        "bandwidth_free_available": 600,
        "bandwidth_paid_available": 0,
        "tron_power": 0,
    })

    # Risk-preflight defaults: make every check pass cleanly so existing
    # /send tests keep working without explicit setup. Tests that
    # actually exercise risk override these locally.
    tron.client.get_account = MagicMock(return_value={
        "create_time": 1700000000_000,
    })
    tron.client.get_contract = MagicMock(side_effect=Exception("not a contract"))
    _usdt_mock = MagicMock()
    _usdt_mock.functions.isBlackListed = MagicMock(return_value=False)
    tron._usdt_contract = _usdt_mock
    tron.get_usdt_contract = MagicMock(return_value=_usdt_mock)
    tron.get_destination_info = MagicMock(return_value={
        "exists": True,
        "trx_balance": Decimal("1"),
        "usdt_balance": Decimal("100"),
    })

    # ── Seed the WalletPool with one mock wallet ───────────────────────
    # Backward-compat aliases: tests historically asserted on
    # ``mock_tron.address`` and assigned mock side-effects via
    # ``mock_tron.send_usdt = MagicMock(...)``. Keep both working in
    # the multi-wallet world by proxying — the actual signing call
    # site is ``wallet.send_usdt(client, contract, to, amount, ...)``;
    # we route that into the legacy ``tron.send_usdt(to, amount, ...)``
    # MagicMock so existing assertions on call_args still match.
    test_address = "TTestAddress1234567890123456789012"
    tron.address = test_address  # legacy read-only alias
    tron.send_usdt = MagicMock(return_value="abc123txid")  # legacy mock target

    fake_wallet = Wallet(
        name="default",
        address=test_address,
        priv_key=MagicMock(),
    )

    # Proxy: drops the (client, contract) prefix so tests that assert
    # ``mock_tron.send_usdt.assert_called_once_with(to, amount, fee_limit_sun=...)``
    # match the actual recorded args.
    def _send_proxy(_client, _contract, to_address, amount, *,
                    fee_limit_sun=None):
        return tron.send_usdt(to_address, amount, fee_limit_sun=fee_limit_sun)
    fake_wallet.send_usdt = _send_proxy

    pool._override_for_tests([fake_wallet])

    yield tron

    pool._reset_for_tests()
    tron.init = original_init
    tron.destroy = original_destroy


@pytest.fixture()
def client(mock_tron):
    """FastAPI TestClient with mocked tron."""
    from contextlib import asynccontextmanager

    from skr_crypto.server.server import create_app

    @asynccontextmanager
    async def test_lifespan(_app):
        yield

    app = create_app()
    app.router.lifespan_context = test_lifespan

    # Reset the bucket-backed rate limiter for test hermeticity.
    # 1.6.0+ uses rate_limit_bucket.limiter; the legacy _limiter._hits
    # dict is unused. We initialise an in-memory bucket here so tests
    # that go through the middleware can hit it; tests that want a
    # specific capacity override `rate_limit_bucket.limiter` themselves.
    from skr_crypto.server import rate_limit_bucket as _rl
    if _rl.limiter is None:
        _rl.limiter = _rl.TokenBucketLimiter(db_path=None)
    else:
        _rl.limiter._reset_for_tests()

    return TestClient(app)


@pytest.fixture()
def auth_headers():
    return {"X-API-Key": "test-secret-token"}


# ---------------------------------------------------------------------------
# Autouse: server-side cleanups so server tests don't interfere
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_idempotency():
    """Reset idempotency store between tests."""
    from skr_crypto.server.idempotency import idempotency
    idempotency._reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Reset Prometheus counters / gauges between tests."""
    from skr_crypto.server import metrics
    for collector in list(metrics.registry._collector_to_names.keys()):
        if hasattr(collector, "_metrics"):
            try:
                collector._metrics.clear()
            except Exception:
                pass
        if hasattr(collector, "_value"):
            try:
                collector._value.set(0)
            except Exception:
                pass
    yield
