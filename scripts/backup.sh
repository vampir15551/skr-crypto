#!/usr/bin/env bash
# Snapshot data/ into ./backups/<timestamp>.tar.gz.
#
# Safe to run while the service is live: the audit log is append-only +
# fsync per record, and SQLite's WAL gives us a consistent read view
# without taking a lock that would block writers.
#
# Override the keep-N count with KEEP=N (default 14 — two weeks of
# daily backups). Override the source dir with DATA=path (default ./data).

set -euo pipefail

cd "$(dirname "$0")/.."

DATA="${DATA:-./data}"
KEEP="${KEEP:-14}"
BACKUPS="./backups"

if [[ ! -d "$DATA" ]]; then
    echo "ERROR: data dir not found: $DATA" >&2
    exit 1
fi

mkdir -p "$BACKUPS"

ts="$(date +%Y%m%d-%H%M%S)"
out="$BACKUPS/payouts-data-$ts.tar.gz"

# Use --exclude on temp WAL/SHM files: they're transient SQLite
# state and not necessary for restore — re-opening the .db rebuilds them.
tar -czf "$out" \
    --exclude='*.db-wal' \
    --exclude='*.db-shm' \
    "$DATA"

echo "✓ wrote $out  ($(du -h "$out" | cut -f1))"

# Prune old backups — keep the newest $KEEP.
mapfile -t old < <(ls -1t "$BACKUPS"/payouts-data-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)))
if [[ ${#old[@]} -gt 0 ]]; then
    for f in "${old[@]}"; do
        rm -f -- "$f"
        echo "  pruned $f"
    done
fi
