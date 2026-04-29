# Static security scans

Each release ships with a fresh pair of scan reports committed to this
directory. They're not policy gates by themselves — review the
findings, mark each as fixed / accepted / annotated, and only then
cut a release. See `RELEASING.md`.

## Tools

| Tool | Scope | Output |
|---|---|---|
| [`bandit`](https://bandit.readthedocs.io) | Python source — looks for known unsafe patterns (eval, hard-coded passwords, shell=True, weak crypto, etc). | `bandit-YYYY-MM-DD.txt` |
| [`pip-audit`](https://pypi.org/project/pip-audit/) | Installed dependency tree vs. the [Python advisory DB](https://github.com/pypa/advisory-database) and OSV. | `pip-audit-YYYY-MM-DD.json` |

Run via `make audits` (or directly):

```bash
bandit -r skr_crypto -ll -f txt -o audits/bandit-$(date +%Y-%m-%d).txt
pip-audit --skip-editable --desc on --format=json \
    -o audits/pip-audit-$(date +%Y-%m-%d).json
```

## Latest report — 2026-04-29 (cut for v1.0.0)

**bandit**

- 0 high
- 2 medium (both annotated and accepted)
- 40 low (mostly subprocess invocations with controlled argv)

The two medium findings:

1. **B104 hardcoded bind 0.0.0.0** in `skr_crypto/server/__main__.py`
   — annotated `# nosec B104`. The `--lan` flag's documented fallback
   when no recognised LAN IP is found. See `OPERATIONS.md`.
2. **B404 subprocess import** in
   `skr_crypto/cli/commands/check_tx.py` and friends — we shell out
   to `sys.executable` for in-process probes. argv is fully
   controlled by us; not a vulnerability.

**pip-audit**

- 0 known vulnerabilities.

The previous run flagged three CVEs that landed before the v1.0.0
cut and were fixed in this release:

- `CVE-2026-28684` (python-dotenv `set_key` symlink follow) — bumped
  to `>=1.2.2`. Not exploitable by us (we only `load_dotenv`),
  bumped for hygiene.
- `CVE-2025-54121` (starlette multipart spool DoS) — pinned starlette
  `>=0.49.1`.
- `CVE-2025-62727` (starlette FileResponse Range header DoS) — same
  pin covers it.

## Process

1. Before tagging a release, re-run both tools and overwrite the
   dated reports.
2. For each new finding: fix in code, or annotate
   (`# nosec <code>` / `--ignore-vuln <id>`) with a one-line rationale
   in the source.
3. Commit the regenerated reports with the change that resolves them
   — the diff makes the audit trail readable.
4. CI does not auto-block on these scans yet; the gate is the
   release checklist in `RELEASING.md`. We may move to
   blocking later if the volume justifies it.

## Out of scope

- **Runtime sandboxing** (apparmor / seccomp / firejail) — handled by
  the systemd unit (`deploy/skr-crypto.service`) and the Dockerfile,
  not these scans.
- **Source-side dependency confusion** — we install from a wheel
  attached to a GitHub Release, never from PyPI; no namespace race
  to worry about.
- **Third-party penetration testing** — no current engagement.
  Anything found goes into a coordinated disclosure per `SECURITY.md`.
