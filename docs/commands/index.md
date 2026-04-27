# Commands overview

Every command is in one of five groups. The rest of this section
documents each in depth; this page is the at-a-glance map.

| Group | Commands |
|---|---|
| [Lifecycle](lifecycle.md) | `install`, `update`, `start`, `stop`, `restart`, `logs` |
| [Inspect](inspect.md) | `status`, `balance`, `check`, `audit`, `reconcile`, `version` |
| [Configuration](configuration.md) | `config show`, `config edit`, `config validate` |
| [Backups](backups.md) | `backup`, `restore` |
| [Diagnostics](diagnostics.md) | `doctor` |

`skr-crypto help` (run on the CLI itself) prints the same list, with
short descriptions, colour-coded by group.

## Common flags

These work on every (or almost every) command:

| Flag | Default | Notes |
|---|---|---|
| `--dir PATH` | `~/.skr-crypto` (or `$SKR_CRYPTO_HOME`) | Override the install dir for this invocation. |
| `--json` | off | Machine-readable output. Bypasses Rich line-wrapping; one record per line for streamed commands like `audit`. |
| `--via systemd / compose / direct` | auto-detect | Force a specific lifecycle regime (relevant only to `start`/`stop`/`restart`/`logs`/`update`). |

## Exit codes

See the [README](../index.md) for the full mapping. Briefly:

- `0` success
- `1` generic failure (last resort)
- `2` not installed
- `3` already installed
- `4` service unreachable
- `5` auth rejected
- `6` bad/malformed API response
- `7` git
- `8` venv / pip
- `9` config error
