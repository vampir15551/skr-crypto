"""``skr-crypto doctor`` — environment checks."""
from __future__ import annotations

import json

from skr_crypto.cli.main import cli


def test_doctor_runs_on_uninstalled(runner, tmp_path):
    """Doctor must run even when nothing is installed — the whole point
    is diagnosing a not-yet-working environment."""
    result = runner.invoke(cli, [
        "--dir", str(tmp_path / "missing"), "doctor",
    ])
    # Some checks will be WARN (no install) but no FAIL on a fresh box;
    # exit code depends on what's installed locally.
    assert result.exit_code in (0, 1)
    assert "python" in result.stdout.lower()
    assert "git" in result.stdout.lower()


def test_doctor_json_emits_array(runner, isolated_install):
    result = runner.invoke(cli, [
        "--dir", str(isolated_install), "doctor", "--json",
    ])
    assert result.exit_code in (0, 1)
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert all({"check", "status", "message"} <= set(c) for c in payload)
    statuses = [c["status"] for c in payload]
    # "ok"/"warn"/"fail" only — no other strings sneak in.
    assert all(s in ("ok", "warn", "fail") for s in statuses)


def test_doctor_warns_on_short_token(runner, tmp_path):
    """A weak AUTH_TOKEN (< 24 chars) must produce a WARN."""
    install = tmp_path / "install"
    install.mkdir()
    (install / "app").mkdir()
    (install / "app" / "__init__.py").write_text("")
    (install / "data").mkdir()
    (install / ".env").write_text("AUTH_TOKEN=short\n")
    import os
    os.chmod(install / ".env", 0o600)

    result = runner.invoke(cli, [
        "--dir", str(install), "doctor", "--json",
    ])
    payload = json.loads(result.stdout)
    secrets_check = next(c for c in payload if c["check"] == "env secrets")
    assert secrets_check["status"] == "warn"
    assert "short" in secrets_check["message"].lower()


def test_doctor_fails_on_unsafe_env_perms(runner, tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    (install / "app").mkdir()
    (install / "app" / "__init__.py").write_text("")
    (install / "data").mkdir()
    (install / ".env").write_text("AUTH_TOKEN=" + "a" * 32 + "\n")
    import os
    os.chmod(install / ".env", 0o644)  # group + other readable

    result = runner.invoke(cli, [
        "--dir", str(install), "doctor", "--json",
    ])
    payload = json.loads(result.stdout)
    perms = next(c for c in payload if c["check"] == "env perms")
    assert perms["status"] == "fail"
    # FAIL anywhere → exit 1.
    assert result.exit_code == 1
