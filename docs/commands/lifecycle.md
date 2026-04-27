# Lifecycle commands

## `install`

Clone the service from git, create a venv, install deps, generate
`AUTH_TOKEN`, write `.env`, create `data/`.

```bash
skr-crypto install [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--git-url URL` | upstream service repo | Where to clone from |
| `--ref REF` | `main` | Branch / tag / commit to check out |
| `--from-path PATH` | (none) | Install from a local working tree (dev) |
| `--force` | off | Wipe an existing install dir first |
| `--interactive` | off | Run the service's own setup wizard |
| `--no-deps` | off | Skip `pip install` (offline / tests) |

A fresh `AUTH_TOKEN` is generated each install — don't expect it to
be stable across re-installs.

## `update`

Pull a new version, snapshot `data/`, restart.

```bash
skr-crypto update [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--version REF` | latest tag | Specific version to update to |
| `-y`, `--yes` | off | Skip the confirmation prompt |
| `--no-backup` | off | Don't snapshot `data/` first |
| `--no-restart` | off | Apply the update but don't restart |
| `--via REGIME` | auto | `systemd` / `compose` / `direct` |

The CHANGELOG diff between current and target is shown before the
prompt, so you see what you're applying.

## `start` / `stop` / `restart`

```bash
skr-crypto start  [--via systemd|compose|direct]
skr-crypto stop   [--via ...]
skr-crypto restart [--via ...]
```

Auto-detects the regime by default. `direct` (no supervisor) cannot
be controlled — start the service manually with `./run.sh`.

## `logs`

```bash
skr-crypto logs [-f] [-n LINES] [--via REGIME] [--audit-only]
```

| Option | Default | Description |
|---|---|---|
| `-f`, `--follow` | off | Tail new lines |
| `-n`, `--lines` | `200` | How many recent lines to show |
| `--audit-only` | off | Tail `data/audit.log` directly (raw JSON) |
| `--via REGIME` | auto | Force regime |

Backed by `journalctl` for systemd, `docker compose logs` for compose,
or `tail` for direct / `--audit-only`. Replaces the python process
with the underlying tool so `Ctrl-C` cleanly stops the tail.
