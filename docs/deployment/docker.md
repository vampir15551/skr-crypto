# Docker deployment

Docker is the recommended Container path for skr-crypto. The image is small, the keys are at rest under AES-256-GCM, and the pattern works identically on Docker Desktop, Compose, k8s, Fly.io, and any other container platform.

## What ships in the image

`Dockerfile` (multi-stage):

- **Base:** `python:3.12-slim` (~50 MB).
- **Adds:** `curl` (for HEALTHCHECK), `ca-certificates`, the package + `[server]` extras.
- **Runs as:** non-root `appuser` (uid 1000).
- **State dir:** `/var/lib/skr-crypto` (mount as a volume).
- **Entrypoint:** `skr-crypto-server`. The CLI `skr-crypto` is also installed for `docker exec` debugging.
- **HEALTHCHECK:** hits `/api/v1/health/live`.

Final image is ~140 MB. No build tools at runtime.

---

## Picking a key provider for containers

| Provider | Container-safe? | Notes |
|---|---|---|
| **`encrypted_file`** | ✅ **Recommended** | Multi-wallet, encrypted at rest, passphrase via secret |
| `env` | ✅ | `PRIVATE_KEY_HEX` via orchestrator secret. Single-wallet legacy or multi-wallet via `WALLETS=`. |
| `file` | ✅ | Mount the chmod-600 key file read-only |
| `1password` | ❌ | Needs `op signin` + biometric — no clean container path |
| `keychain` | ❌ | macOS-only |

The rest of this page assumes `encrypted_file`. For the others, the wiring is similar — see [Key providers](../key-providers.md) for per-backend specifics.

---

## docker-compose.yml in this repo

The shipped `docker-compose.yml` is a working starting point. Three things you must configure before `docker compose up`:

1. **`.env`** — `AUTH_TOKEN`, `KEY_PROVIDER`, `TRON_NETWORK`, `TRONGRID_API_KEY`, etc.
2. **The keystore file** at `./data/keystore.json` (created with `skr-crypto wallet encrypt` from a host venv).
3. **The passphrase** — either in `.env` as `KEY_PASSPHRASE`, or as a chmod-600 file mounted read-only.

```yaml
# docker-compose.yml (excerpt — see file for the complete version)
services:
  skr-crypto:
    build:
      context: .
      args:
        GIT_SHA: ${GIT_SHA:-unknown}
    image: skr-crypto:latest
    restart: unless-stopped
    env_file:
      - .env
    environment:
      SERVER_HOST: 0.0.0.0
      SKR_CRYPTO_HOME: /var/lib/skr-crypto
      KEYSTORE_FILE: /var/lib/skr-crypto/data/keystore.json
      SHUTDOWN_TIMEOUT: "31536000"      # ~1 year, effectively off
    ports:
      - "127.0.0.1:8000:8000"            # host loopback only
    volumes:
      - ./data:/var/lib/skr-crypto/data  # keystore, audit, idempotency
    healthcheck:
      test: ["CMD", "curl", "-f", "-s", "http://127.0.0.1:8000/api/v1/health/live"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    deploy:
      resources:
        limits:
          memory: 256M
          cpus: "0.50"
```

---

## End-to-end first deploy

### One-command bootstrap (recommended)

```bash
git clone https://github.com/vampir15551/skr-crypto.git
cd skr-crypto
./scripts/bootstrap-docker.sh
```

The bootstrap script:

1. **Pre-flight** — verifies Docker daemon is running; refuses to
   overwrite an existing `.env` unless `BOOTSTRAP_FORCE=1`.
2. **Secret generation** — `AUTH_TOKEN`, `KEY_PASSPHRASE`,
   `WEBHOOK_SIGNING_SECRET` via `openssl rand -hex 32`.
3. **`.env` write** — chmod-600, with sane local-dev defaults
   (encrypted_file backend, mainnet, audit log + idempotency DB
   on the bind-mounted `./data` volume).
4. **Image build** — `docker compose build skr-crypto`. ~1-2 min
   on a fresh machine, ~5s with cache.
5. **Keystore init** — runs `skr-crypto wallet encrypt` inside an
   ephemeral container (no Python needed on the host). Uses the
   `KEY_PASSPHRASE` env var to skip the interactive double-confirm.
6. **Wallet generation** — `skr-crypto wallet generate main` (also
   in-container).
7. **Service start** — `docker compose up -d`, with healthcheck.
8. **Health probe wait** — polls `/health/live` for up to 60 s.
9. **Summary** — prints the UI URL, the AUTH_TOKEN to paste, the
   newly-created wallet's address, and common ops commands.

Re-run with `BOOTSTRAP_FORCE=1` to wipe `.env` + `./data/` and start
over.

After the script finishes:

- **UI**: <http://127.0.0.1:8000/ui/> — paste the printed token.
- **Wallet `main`**: fund the printed address with ≥50 TRX (for
  fees) + the USDT you want to send.
- **First /send**: copy the curl example from the script's summary.

### Manual (for environments where you can't run the script)

```bash
git clone https://github.com/vampir15551/skr-crypto.git
cd skr-crypto

# 1. Install locally (just enough to use the CLI for keystore management).
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[server]"

# 2. Copy + edit .env.
cp .env.example .env
# Edit AUTH_TOKEN, TRON_NETWORK, TRONGRID_API_KEY, KEY_PROVIDER=encrypted_file
# Set KEYSTORE_FILE=data/keystore.json (relative to the install dir; container mounts ./data)

# 3. Initialise the keystore + add a wallet — on the HOST, not in the container.
skr-crypto wallet encrypt -o data/keystore.json
skr-crypto wallet generate main

# 4. Decide how to provide the passphrase.
#    Option A — env var (simplest):
echo "KEY_PASSPHRASE=your-passphrase-here" >> .env

#    Option B — file mount (more secret-manager-friendly):
mkdir -p secrets
echo "your-passphrase-here" > secrets/keystore.pass
chmod 600 secrets/keystore.pass
echo "KEY_PASSPHRASE_FILE=/run/secrets/keystore.pass" >> .env
# Then add to docker-compose.yml volumes:
#   - ./secrets/keystore.pass:/run/secrets/keystore.pass:ro

# 5. Start.
docker compose up -d --build

# 6. Verify.
curl http://127.0.0.1:8000/api/v1/health/live
docker compose logs -f skr-crypto
```

---

## Operations inside the container

```bash
# Logs
docker compose logs -f skr-crypto

# Live wallet listing (CLI inside container)
docker compose exec skr-crypto skr-crypto wallet list --offline

# Tail audit
docker compose exec skr-crypto tail -f /var/lib/skr-crypto/data/audit.log

# Restart (after adding wallets / changing config)
docker compose restart skr-crypto
```

The CLI works inside the container the same as on the host. Mutating commands (`wallet add`, `wallet generate`) modify the same volume your service reads from — but **you must restart** for the new wallet to land in the pool.

---

## Adding a wallet to a live deploy

```bash
# Either: from the host (the venv with skr-crypto installed)
KEY_PASSPHRASE='your-passphrase-here' \
  skr-crypto wallet generate cold

# Or: inside the container
docker compose exec skr-crypto skr-crypto wallet generate cold
# (Will prompt for passphrase via TTY)

# Restart the service:
docker compose restart skr-crypto

# Verify:
curl -H "X-API-Key: $AUTH_TOKEN" http://127.0.0.1:8000/api/v1/wallets
```

---

## Putting Caddy / nginx in front

The compose file binds the service to `127.0.0.1:8000` — only loopback. In production you want TLS + a domain. Caddy is the smallest sensible answer:

```caddyfile
api.example.com {
    reverse_proxy 127.0.0.1:8000

    # Restrict by source IP if your callers are known.
    @allowed remote_ip 203.0.113.0/24
    handle @allowed {
        reverse_proxy 127.0.0.1:8000
    }
    respond 403
}
```

Make sure `TRUSTED_PROXIES` in `.env` includes Caddy's IP (`127.0.0.1` if same host, the LAN IP otherwise) so the rate limiter and audit log read `X-Forwarded-For` correctly.

For the systemd-managed Caddy + skr-crypto pattern, see [VPS + systemd](systemd-vps.md).

---

## Rotating the passphrase

The keystore's salt + per-entry IVs are tied to the current passphrase. To rotate:

```bash
# Stop the service.
docker compose stop skr-crypto

# Decrypt + re-init under a new passphrase.
# This is destructive — the old salt is replaced.
KEY_PASSPHRASE='OLD' skr-crypto wallet list --offline   # confirm what you have
# Export each wallet's hex (needs current passphrase):
KEY_PASSPHRASE='OLD' python -c "
from skr_crypto.server.encrypted_keystore import load_keystore
for e in load_keystore('data/keystore.json', b'OLD'):
    print(e.name, bytes(e.raw_key).hex())
"

# Init fresh under the new passphrase.
skr-crypto wallet encrypt -o data/keystore.json.new --force
# Re-add each wallet under the new passphrase using --hex.
skr-crypto wallet add main --hex <hex-from-above> --yes  # (uses new passphrase)
# ... (repeat per wallet)

# Swap files.
mv data/keystore.json.new data/keystore.json

# Update .env passphrase, restart.
docker compose up -d
```

A first-class `wallet rotate-passphrase` command isn't shipped yet. Do this carefully and back up first.

---

## Resource sizing

Defaults:

- **Memory:** 256 MB limit. Reality: ~80 MB resident in steady state.
- **CPU:** 0.5 vCPU limit. Reality: <5% under typical /send rates.

If you serve many wallets (>50), bump memory to 512 MB. The bottleneck is the cryptography library's scrypt instance during boot — multi-second on tiny instances. For routine /send traffic the resource floor is irrelevant.

---

## Healthcheck behaviour

`HEALTHCHECK` hits `/api/v1/health/live` (unauthenticated) every 30 s. The service is marked unhealthy if it fails 3 in a row.

`docker compose up -d` waits for healthy before considering the service started, but only with `depends_on: { skr-crypto: { condition: service_healthy } }` from a dependent service. Plain `up -d` returns immediately.

---

## Backup strategy

You need to back up:

```text
data/
├── keystore.json       — the AES-256-GCM keystore
├── audit.log           — append-only financial trail
└── idempotency.db      — SQLite WAL idempotency state
```

A daily rsync to a separate host is enough. The CLI ships:

```bash
docker compose exec skr-crypto skr-crypto backup
# Writes /var/lib/skr-crypto/data/backup-2026-04-30T12-00-00.tar.gz
```

The tarball is `chmod 600`. Pull it off the host immediately — leaving the only backup on the same host as the service is the same as no backup.

**Restore** is `skr-crypto restore <tarball>` — same machine or another. The wallet pool reloads on next service restart.

---

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| `Keystore passphrase rejected` on container start | `KEY_PASSPHRASE` missing or wrong | Check `.env`; check that `env_file:` is set in compose |
| `KEYSTORE_FILE not found` | Volume mount path mismatch | Make sure `./data:/var/lib/skr-crypto/data` and `KEYSTORE_FILE=/var/lib/skr-crypto/data/keystore.json` align |
| `[file] has unsafe mode` | Keystore copied from another host with looser perms | `chmod 600 data/keystore.json` on host |
| `WalletPool initialised with zero wallets` | Empty keystore | Run `wallet generate` before the service starts |
| Container restart loop, no useful logs | Healthcheck failing because port mismatch | Check `SERVER_PORT` matches the EXPOSE / HEALTHCHECK port |
| `429` from caller's outside-network requests | `TRUSTED_PROXIES` doesn't list the reverse proxy IP | Add the proxy IP; restart |

---

## See also

- [Key providers](../key-providers.md) — non-`encrypted_file` patterns inside containers
- [Encrypted keystore format](../keystore-format.md) — the file format the volume holds
- [Monitoring & metrics](../monitoring.md) — Prometheus inside or outside the container
- [VPS + systemd](systemd-vps.md) — non-container production deployment
