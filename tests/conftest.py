"""Shared fixtures.

Two principles for the test suite:

  1. **Never touch the user's real install dir.** Every command that
     reads ``~/.skr-crypto`` is invoked with ``--dir tmp_path`` (or
     ``SKR_CRYPTO_HOME=tmp_path``). The ``isolated_install`` fixture
     materialises a minimal fake install for that.
  2. **No real network.** ``responses`` mocks all HTTP. Any test that
     forgets to mock something will hit a real socket — they must
     fail loudly, not flake silently. We don't add a global "block
     all sockets" hook because it would interfere with subprocess-
     spawned tests; instead each HTTP test wires ``responses`` itself.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    """Click's test runner. Captures stdout/stderr and exit code.

    Click 8.2 dropped the ``mix_stderr`` arg and split stderr by default,
    which is what we want — tests assert on stdout (data) vs stderr
    (status) independently.
    """
    return CliRunner()


@pytest.fixture
def isolated_install(tmp_path: Path) -> Path:
    """Create a minimal but valid 'installed' service at tmp_path.

    Includes:
      - .env with safe defaults + a known AUTH_TOKEN
      - app/ stub (just an __init__) so ``require_installed`` accepts it
      - data/ dir
      - venv/bin/python pointing at the real interpreter so ``check``
        / ``reconcile`` could in theory run (tests mock subprocess
        anyway).
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

    # Fake venv: a directory with bin/python, mode 755, just to satisfy
    # the existence check in commands that touch venv_python. We never
    # actually invoke this binary in tests — those that would are mocked.
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    fake_py = tmp_path / "venv" / "bin" / "python"
    fake_py.write_text("#!/bin/sh\necho FAKE\n")
    os.chmod(fake_py, 0o755)

    return tmp_path


@pytest.fixture
def base_url() -> str:
    """The URL the APIClient will compute from the isolated_install env."""
    return "http://127.0.0.1:8765"
