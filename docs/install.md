# Install

The CLI is a Python package. Install it via pip from PyPI:

```bash
pip install skr-crypto
```

For development (clone + editable install):

```bash
git clone https://github.com/vampir15551/skr-crypto.git
cd skr-crypto
python3.12 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
```

## Bootstrap a service install

Once the CLI itself is on the box, install the underlying service:

```bash
skr-crypto install
```

By default this:

1. Clones the service repo into `~/.skr-crypto`.
2. Creates a virtualenv inside it.
3. Installs runtime dependencies.
4. Generates a fresh high-entropy `AUTH_TOKEN`.
5. Writes a chmod-600 `.env` with safe defaults.
6. Creates `data/` for the audit log + idempotency DB.

You'll see the token printed on success — save it; your API clients
need it as the `X-API-Key` header.

## Customising the install

```bash
# Different install path (also via $SKR_CRYPTO_HOME)
skr-crypto --dir /opt/skr-crypto install

# Pin to a specific version
skr-crypto install --ref v0.3.0

# Install from a local working tree (dev)
skr-crypto install --from-path ../skr_crypto-payouts

# Wipe an existing dir and start over
skr-crypto install --force

# Run the service's own interactive setup wizard
skr-crypto install --interactive

# Skip pip (offline / tests)
skr-crypto install --no-deps
```

## Setting the private key

The default `KEY_PROVIDER` is `env`, which expects `PRIVATE_KEY_HEX`
in the environment when the service starts. Other providers are
documented in [Configuration](configuration.md):

```bash
# Option A: env (default — recommended for containers)
export PRIVATE_KEY_HEX='<32-byte hex>'

# Option B: file (chmod 600, recommended for VPS systemd deploys)
skr-crypto config edit  # set KEY_PROVIDER=file, PRIVATE_KEY_FILE=/etc/skr-crypto/key
sudo install -m 600 -o $USER /dev/stdin /etc/skr-crypto/key <<< '<32-byte hex>'

# Option C: 1Password / Keychain — see configuration.md
```

## Starting the service

```bash
skr-crypto start
skr-crypto status
```

`start` auto-detects the lifecycle regime:

- **systemd** if a `payouts.service` unit references the install dir
- **docker compose** if `docker-compose.yml` lives next to the install
- **direct** otherwise — the CLI can't supervise the process; you
  start `./run.sh` yourself in the install dir

## Verifying

```bash
# Should print the running version + uptime
skr-crypto status

# Sanity: balances reachable
skr-crypto balance

# Doctor — validates the whole environment
skr-crypto doctor
```

`doctor` exits 1 if any check FAILs, so it's safe to drop into CI.

## Updating

```bash
# Latest tagged release
skr-crypto update

# Pinned version
skr-crypto update --version v0.3.0

# Skip the confirmation
skr-crypto update --yes

# Don't restart automatically
skr-crypto update --no-restart
```

`update` shows the CHANGELOG diff between current and target before
applying, snapshots `data/` first (unless `--no-backup`), and restarts
via the detected regime.
