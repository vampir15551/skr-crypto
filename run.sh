#!/usr/bin/env bash
# Local-dev launcher: unlock 1Password (if KEY_PROVIDER=1password is in
# play), optionally start a Cloudflare quick tunnel that gives you a
# public https://*.trycloudflare.com URL pointing at the local API,
# then start the service. Prod deploys go through systemd / docker
# compose, not this script.
#
# Flags forwarded to skr-crypto-server: --lan, --host, --port (see
# `skr-crypto-server --help`). Anything else is also passed through.

set -euo pipefail

cd "$(dirname "$0")"

# ── 1Password unlock (only if op is on PATH) ──────────────────────────
# Skip silently if op isn't installed; the service will fail at
# load_private_key time with a clearer error if KEY_PROVIDER=1password
# is configured.
if command -v op >/dev/null 2>&1; then
    echo "Unlocking 1Password (confirm with Touch ID or password)..."
    eval "$(op signin)"
    echo "1Password unlocked"
fi

# ── Resolve server port (env > .env > --port arg > default 8000) ─────
PORT="${SERVER_PORT:-}"
if [[ -z "$PORT" && -f .env ]]; then
    PORT="$(grep -E '^SERVER_PORT=' .env | tail -n1 | cut -d= -f2 | tr -d '"' | tr -d "'" || true)"
fi
prev=""
for arg in "$@"; do
    [[ "$prev" == "--port" ]] && PORT="$arg"
    [[ "$arg" == --port=* ]] && PORT="${arg#--port=}"
    prev="$arg"
done
PORT="${PORT:-8000}"

# ── Optional: Cloudflare quick tunnel ────────────────────────────────
# `cloudflared tunnel --url http://localhost:$PORT` gives a temporary
# public https URL. Useful for testing webhooks from a partner who
# can't reach your laptop. Disable with NO_TUNNEL=1.
CF_PID=""
CF_LOG=""
if [[ "${NO_TUNNEL:-}" != "1" ]] && command -v cloudflared >/dev/null 2>&1; then
    CF_LOG="$(mktemp -t cloudflared.XXXXXX.log)"
    cloudflared tunnel --no-autoupdate --url "http://localhost:${PORT}" \
        >"$CF_LOG" 2>&1 &
    CF_PID=$!

    cleanup() {
        if [[ -n "$CF_PID" ]] && kill -0 "$CF_PID" 2>/dev/null; then
            kill "$CF_PID" 2>/dev/null || true
            wait "$CF_PID" 2>/dev/null || true
        fi
        [[ -n "$CF_LOG" ]] && rm -f "$CF_LOG"
    }
    trap cleanup EXIT INT TERM

    echo "Starting Cloudflare tunnel for http://localhost:${PORT} ..."
    CF_URL=""
    for _ in $(seq 1 60); do
        CF_URL=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$CF_LOG" \
                 | head -n1 || true)
        [[ -n "$CF_URL" ]] && break
        kill -0 "$CF_PID" 2>/dev/null || break
        sleep 0.5
    done

    if [[ -n "$CF_URL" ]]; then
        echo
        echo "================================================================"
        echo "  Cloudflare tunnel: $CF_URL"
        echo "  Log: $CF_LOG"
        echo "================================================================"
        echo
    else
        echo "WARNING: Cloudflare tunnel URL not detected — see $CF_LOG"
    fi
elif [[ "${NO_TUNNEL:-}" == "1" ]]; then
    : # explicitly disabled, no message
elif ! command -v cloudflared >/dev/null 2>&1; then
    echo "Note: cloudflared not installed — skipping public tunnel."
    echo "      brew install cloudflared   (or set NO_TUNNEL=1 to silence)"
fi

# ── Start the service ────────────────────────────────────────────────
echo "Starting SKR Crypto Treasury Service..."
exec skr-crypto-server "$@"
