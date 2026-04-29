#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# ── Activate venv ───────────────────────────────────────────────────────
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
elif [ -f venv/bin/activate ]; then
    source venv/bin/activate
fi

# ── Wallet 1 (sender) ──────────────────────────────────────────────────
export TRON_E2E=1
export E2E_PRIVATE_KEY="8e8059e4590bc8e548dce30df6ade51c0bfc802181d3d26054bd1774da8a9685"
export E2E_TO_ADDRESS="TAVZ1gRhZMWN5XaMYLCzA234QVziWcQKiP"

echo "=== TRON Nile E2E Tests ==="
echo "Sender  : TPXnaoL6dauQqjE7KKQnhQwJqYvyPvCYJu"
echo "Receiver: $E2E_TO_ADDRESS"
echo ""

python3 -m pytest tests/test_e2e_testnet.py -v -s "$@"
