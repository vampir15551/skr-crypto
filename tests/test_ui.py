"""Tests for the read-only operator UI (1.8.0).

Covers:

  - Static bundle is served at /ui/ and includes index.html
  - The bundle contains NO money-moving API calls (invariant!)
  - The audit pagination endpoint works end-to-end
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

UI_DIR = Path(__file__).parent.parent / "skr_crypto" / "server" / "ui" / "static"


# ---------------------------------------------------------------------------
# Static bundle invariants
# ---------------------------------------------------------------------------


class TestUIBundle:
    @pytest.mark.invariant
    def test_bundle_contains_no_send_post(self):
        """INVARIANT (ADR 0012): the UI bundle MUST NOT contain a
        POST to /api/v1/send. Money paths are exclusively the
        authenticated HTTP API. If this regresses, someone added a
        money-moving form to the read-only UI — undo it.
        """
        bad_patterns = [
            r"/api/v1/send",
            r"['\"]POST['\"].*?send",
            # Reasonable canaries for a future write-op slip
            r"method:\s*['\"]POST['\"]",
        ]
        offenses = []
        for path in UI_DIR.glob("*.js"):
            content = path.read_text()
            for pat in bad_patterns:
                # Allow the pattern in COMMENTS (// or /* */) — those
                # documentation strings are how we got the invariant.
                # We strip comments before scanning.
                stripped = _strip_js_comments(content)
                if re.search(pat, stripped):
                    offenses.append((path.name, pat, _excerpt(stripped, pat)))
        assert not offenses, (
            "UI bundle contains money-moving call(s):\n"
            + "\n".join(f"  {f}: pattern {p!r} → {e!r}" for f, p, e in offenses)
        )

    @pytest.mark.invariant
    def test_bundle_contains_no_inline_event_handlers(self):
        """INVARIANT: no inline `onclick=`/`onload=` etc. — they bypass
        the CSP we want to add in production. Alpine.js's `@click`
        directives are fine."""
        idx = (UI_DIR / "index.html").read_text()
        offenses = re.findall(r"\bon(?:click|load|error|submit|change|input)\s*=", idx)
        assert not offenses, (
            f"index.html has inline event handlers: {offenses}. "
            "Use Alpine.js @click etc. instead."
        )

    def test_bundle_files_present(self):
        for f in ("index.html", "app.js", "style.css", "alpine.min.js"):
            assert (UI_DIR / f).exists(), f"missing UI asset: {f}"


# ---------------------------------------------------------------------------
# UI mount + static serve
# ---------------------------------------------------------------------------


class TestUIMount:
    def test_ui_root_serves_index_html(self, client):
        # No auth needed for the static HTML — auth is on /api/v1/* only
        r = client.get("/ui/")
        assert r.status_code == 200
        assert "<title>SKR Crypto" in r.text
        assert "Operator UI" in r.text

    def test_ui_static_assets_served(self, client):
        r = client.get("/ui/style.css")
        assert r.status_code == 200
        assert "var(--accent)" in r.text
        r = client.get("/ui/app.js")
        assert r.status_code == 200
        # 1.8.2 switched from a global `function app()` to the
        # canonical Alpine 3 `Alpine.data('app', ...)` registration.
        assert "Alpine.data('app'" in r.text

    def test_alpine_bundled(self, client):
        r = client.get("/ui/alpine.min.js")
        assert r.status_code == 200
        # Sanity: this should be the minified Alpine.js
        assert len(r.content) > 10000


# ---------------------------------------------------------------------------
# /api/v1/audit pagination endpoint
# ---------------------------------------------------------------------------


class TestAuditEndpoint:
    def test_no_audit_file_returns_warning(self, client, auth_headers, monkeypatch):
        # Force AUDIT_LOG_FILE empty
        import skr_crypto.server.config as cfg
        monkeypatch.setattr(cfg, "AUDIT_LOG_FILE", "")
        r = client.get("/api/v1/audit", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["records"] == []
        assert "warning" in body

    def test_reads_records_from_audit_file(
        self, client, auth_headers, monkeypatch, tmp_path,
    ):
        # Write a synthetic audit file
        log = tmp_path / "audit.log"
        import json
        records = [
            {"id": "a-1", "event": "SEND_SUCCESS", "wallet": "main",
             "to_address": "T1", "amount": "10", "result": "broadcast",
             "timestamp": "2026-04-30T08:00:00+00:00", "txid": "tx1"},
            {"id": "a-2", "event": "SEND_REJECTED", "wallet": "main",
             "to_address": "T2", "amount": "5", "result": "risk_too_high",
             "timestamp": "2026-04-30T08:01:00+00:00", "txid": ""},
            {"id": "a-3", "event": "SEND_SUCCESS", "wallet": "cold",
             "to_address": "T3", "amount": "100", "result": "broadcast",
             "timestamp": "2026-04-30T08:02:00+00:00", "txid": "tx3"},
        ]
        log.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        import skr_crypto.server.config as cfg
        monkeypatch.setattr(cfg, "AUDIT_LOG_FILE", str(log))

        r = client.get("/api/v1/audit?limit=10", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert len(body["records"]) == 3
        # Most recent first
        assert body["records"][0]["id"] == "a-3"

    def test_event_filter(self, client, auth_headers, monkeypatch, tmp_path):
        log = tmp_path / "audit.log"
        import json
        log.write_text("\n".join(json.dumps(r) for r in [
            {"id": "a-1", "event": "SEND_SUCCESS", "wallet": "main"},
            {"id": "a-2", "event": "SEND_REJECTED", "wallet": "main"},
            {"id": "a-3", "event": "SEND_SUCCESS", "wallet": "cold"},
        ]) + "\n")
        import skr_crypto.server.config as cfg
        monkeypatch.setattr(cfg, "AUDIT_LOG_FILE", str(log))

        r = client.get(
            "/api/v1/audit?event=SEND_SUCCESS&limit=10",
            headers=auth_headers,
        )
        assert r.status_code == 200
        events = {rec["event"] for rec in r.json()["records"]}
        assert events == {"SEND_SUCCESS"}

    def test_audit_endpoint_requires_read_scope(self, client):
        # Without auth → 401
        r = client.get("/api/v1/audit")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_js_comments(s: str) -> str:
    # Strip /* ... */ and // ... block + line comments.
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r"//[^\n]*", "", s)
    return s


def _excerpt(s: str, pat: str, ctx: int = 30) -> str:
    m = re.search(pat, s)
    if not m:
        return ""
    a = max(0, m.start() - ctx)
    b = min(len(s), m.end() + ctx)
    return s[a:b].replace("\n", " ")
