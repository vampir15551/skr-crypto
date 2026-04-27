# Diagnostics

## `doctor`

```bash
skr-crypto doctor [--json]
```

`brew doctor`-style health walk. Designed to be greppable and
CI-friendly: exits 1 on any FAIL, 0 otherwise, status strings stable
across releases.

Current checks:

| Check | Status | Notes |
|---|---|---|
| `python` | FAIL if <3.11 | Service requires 3.11+ |
| `git` | FAIL if missing | Needed by `install` / `update` |
| `disk space` | WARN <1GB / FAIL <200MB | Where the install dir would land |
| `install` | WARN if absent | Tells you to run `install` |
| `env perms` | FAIL if not 0600 | Group/other readable .env is unsafe |
| `env secrets` | FAIL if AUTH_TOKEN unset; WARN if short / TRONGRID_API_KEY unset | |
| `key provider` | FAIL on misconfig per provider | See per-row hints |
| `data dir` | WARN if missing | Auto-created on first `/send`, but odd if absent |
| `not as root` | WARN if root | Run as a regular user |

JSON output:

```json
[
  {"check": "python", "status": "ok",   "message": "Python 3.12.3"},
  {"check": "git",    "status": "ok",   "message": "git version 2.45"},
  {"check": "env perms", "status": "fail", "message": ".env mode 644 — group/other can read it"}
]
```

Wire it into your monitoring or post-deploy CI step:

```bash
skr-crypto doctor --json | jq -e 'all(.status != "fail")'
```
