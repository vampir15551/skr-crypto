# Backup / restore

The service keeps state on disk: `data/audit.log` (durable JSON-line
audit) and `data/idempotency.db` (SQLite WAL store). These commands
snapshot and restore that directory.

## `backup`

```bash
skr-crypto backup [--out PATH] [--keep N]
```

Creates `<install>/backups/data-YYYYMMDD-HHMMSS.tar.gz`. Safe to run
while the service is up:

- Audit log is append + fsync per record, so the archive is internally
  consistent.
- SQLite is in WAL mode, so the read in tar doesn't block writers.
- WAL/SHM files are excluded — they're transient and reopening the
  `.db` rebuilds them.

| Option | Default | Description |
|---|---|---|
| `--out PATH` | `<install>/backups` | Where to write the archive |
| `--keep N` | `14` | Retain at most N most-recent backups |

## `restore`

```bash
skr-crypto restore <archive> [-y]
```

Restores `data/` from a `.tar.gz` produced by `backup`. The current
`data/` is moved aside to `data.pre-restore-<ts>/` first — a botched
restore is recoverable.

Path traversal is rejected (an archive that tries to extract outside
the install dir fails loudly). The service should be stopped first;
the command warns but doesn't enforce, since some failure modes are
diagnosable only with the service down.

```bash
# After a bad upgrade
skr-crypto stop
skr-crypto restore backups/data-20260427-120000.tar.gz --yes
skr-crypto start
```
