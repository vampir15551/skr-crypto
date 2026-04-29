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
    """Patch the tron singleton with predictable return values."""
    from skr_crypto.server.tron_client import tron

    original_init = tron.init
    original_destroy = tron.destroy

    tron.init = MagicMock()
    tron.destroy = MagicMock()
    tron.address = "TTestAddress1234567890123456789012"
    tron.priv_key = MagicMock()
    tron.client = MagicMock()
    tron._usdt_contract = MagicMock()

    tron.get_trx_balance = MagicMock(return_value=Decimal("100"))
    tron.get_usdt_balance = MagicMock(return_value=Decimal("5000"))
    tron.send_usdt = MagicMock(return_value="abc123txid")
    tron.check_connection = MagicMock(return_value=True)

    tron.estimate_transfer_energy = MagicMock(return_value=None)
    tron.energy_price_sun = MagicMock(return_value=420)
    tron.compute_fee_limit_sun = MagicMock(return_value=30_000_000)
    tron.get_resource_summary = MagicMock(return_value={
        "energy_available": 0,
        "energy_limit": 0,
        "bandwidth_free_available": 600,
        "bandwidth_paid_available": 0,
        "tron_power": 0,
    })

    # Risk-preflight defaults (added in v1.2). Make every check pass
    # cleanly so existing /send tests keep working without explicit
    # setup. Tests that actually exercise risk override these locally.
    tron.client.get_account = MagicMock(return_value={
        "create_time": 1700000000_000,
    })
    tron.client.get_contract = MagicMock(side_effect=Exception("not a contract"))
    # `tron._get_usdt_contract()` returns a contract mock whose
    # `.functions.isBlackListed(addr)` returns False by default.
    # Wire both the attribute and the method (risk module calls the
    # method; the existing send tests poke the attribute directly).
    _usdt_mock = MagicMock()
    _usdt_mock.functions.isBlackListed = MagicMock(return_value=False)
    tron._usdt_contract = _usdt_mock
    tron._get_usdt_contract = MagicMock(return_value=_usdt_mock)
    tron.get_destination_info = MagicMock(return_value={
        "exists": True,
        "trx_balance": Decimal("1"),
        "usdt_balance": Decimal("100"),
    })

    yield tron

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

    from skr_crypto.server.server import _limiter
    _limiter._hits.clear()

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
