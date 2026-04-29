"""Tests for durable audit trail (AUDIT_LOG_FILE writing with fsync)."""
from __future__ import annotations

import importlib
import json


def test_audit_writes_to_durable_file(tmp_path, monkeypatch):
    """Records written to audit.record should also land on disk, one per line,
    parsable as JSON, in the order they were written."""
    audit_file = tmp_path / "audit.log"

    # Configure the env BEFORE (re-)importing the audit module so the module
    # reads AUDIT_LOG_FILE on import.
    monkeypatch.setenv("AUDIT_LOG_FILE", str(audit_file))

    import skr_crypto.server.config as cfg
    importlib.reload(cfg)
    import skr_crypto.server.audit as audit_mod
    importlib.reload(audit_mod)

    audit_mod.record("EVENT_A", amount="1", result="ok")
    audit_mod.record("EVENT_B", amount="2", result="ok")
    audit_mod.close_audit_file()

    lines = [ln for ln in audit_file.read_text().splitlines() if ln.strip()]
    # First line is the audit_init marker written on first open.
    parsed = [json.loads(ln) for ln in lines]
    events = [e.get("event") for e in parsed if "event" in e]
    assert events == ["EVENT_A", "EVENT_B"]

    # Every data line has an id with the process prefix pattern
    for e in parsed:
        if "event" in e:
            assert "id" in e and "-" in e["id"]

    # Reset so other tests don't try to reopen the (now-closed) file.
    monkeypatch.delenv("AUDIT_LOG_FILE", raising=False)
    importlib.reload(cfg)
    importlib.reload(audit_mod)


def test_audit_file_disabled_when_env_empty(tmp_path, monkeypatch):
    """Without AUDIT_LOG_FILE, record() must not attempt any disk IO."""
    monkeypatch.delenv("AUDIT_LOG_FILE", raising=False)

    import skr_crypto.server.config as cfg
    importlib.reload(cfg)
    import skr_crypto.server.audit as audit_mod
    importlib.reload(audit_mod)

    # Just verify no exceptions and _file_fp stays None.
    audit_mod.record("NOFILE", result="ok")
    assert audit_mod._file_fp is None


def test_audit_file_open_failure_raises(tmp_path, monkeypatch):
    """If AUDIT_LOG_FILE is configured but the file can't be opened (bad
    path, perms, full disk), record() must raise AuditWriteError. A
    money-mover with a configured-but-broken audit trail must fail
    loudly — the request returns 5xx and the operator notices."""
    import pytest

    bad_path = tmp_path / "nonexistent_dir" / "audit.log"  # parent doesn't exist
    monkeypatch.setenv("AUDIT_LOG_FILE", str(bad_path))

    import skr_crypto.server.config as cfg
    importlib.reload(cfg)
    import skr_crypto.server.audit as audit_mod
    importlib.reload(audit_mod)

    with pytest.raises(audit_mod.AuditWriteError):
        audit_mod.record("EVENT", result="ok")
    assert audit_mod._file_open_failed is True

    # Subsequent calls also raise — refusing to silently continue.
    with pytest.raises(audit_mod.AuditWriteError):
        audit_mod.record("EVENT2", result="ok")

    monkeypatch.delenv("AUDIT_LOG_FILE", raising=False)
    importlib.reload(cfg)
    importlib.reload(audit_mod)


def test_audit_fsync_failure_raises(tmp_path, monkeypatch):
    """If the audit file is open but fsync/write later fails (volume gone
    read-only, disk full mid-run), record() must raise AuditWriteError."""
    import pytest

    audit_file = tmp_path / "audit.log"
    monkeypatch.setenv("AUDIT_LOG_FILE", str(audit_file))

    import skr_crypto.server.config as cfg
    importlib.reload(cfg)
    import skr_crypto.server.audit as audit_mod
    importlib.reload(audit_mod)

    # First write opens the file successfully.
    audit_mod.record("FIRST", result="ok")

    # Now break the file handle: replace it with a stub whose write raises.
    class _BrokenFile:
        def write(self, _):
            raise OSError("ENOSPC: no space left on device")

        def flush(self):
            raise OSError("ENOSPC")

        def fileno(self):
            return -1

    audit_mod._file_fp = _BrokenFile()

    with pytest.raises(audit_mod.AuditWriteError):
        audit_mod.record("SECOND", result="ok")

    # Restore so close_audit_file in module unload doesn't choke.
    audit_mod._file_fp = None
    monkeypatch.delenv("AUDIT_LOG_FILE", raising=False)
    importlib.reload(cfg)
    importlib.reload(audit_mod)


def test_audit_no_durable_no_raise(tmp_path, monkeypatch):
    """When AUDIT_LOG_FILE is empty, record() must NOT raise — durable
    audit is opt-in, and the operator may legitimately run without it."""
    monkeypatch.delenv("AUDIT_LOG_FILE", raising=False)

    import skr_crypto.server.config as cfg
    importlib.reload(cfg)
    import skr_crypto.server.audit as audit_mod
    importlib.reload(audit_mod)

    # No exception, no file handle.
    audit_mod.record("OPT_OUT", result="ok")
    assert audit_mod._file_fp is None
    assert audit_mod._file_open_failed is False
