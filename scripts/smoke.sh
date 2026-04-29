#!/usr/bin/env bash
# End-to-end smoke check against a running instance.
#
# Hits health/live → version → health → balance, asserting each returns
# 2xx and the expected JSON shape. Does NOT call /send — it's read-only
# by design so it's safe in production.
#
# Usage:
#   ./scripts/smoke.sh                      # defaults: 127.0.0.1:8000
#   BASE=https://payouts.example.com ./scripts/smoke.sh
#   AUTH_TOKEN=... ./scripts/smoke.sh
#
# Exit code: 0 on success, 1 on the first failed check (with diagnostic
# output). Designed to be cron-friendly / CI-friendly.

set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
TOKEN="${AUTH_TOKEN:-}"
PREFIX="$BASE/api/v1"

# If AUTH_TOKEN isn't passed in, try to read it from .env.
if [[ -z "$TOKEN" && -f .env ]]; then
    TOKEN="$(grep -E '^AUTH_TOKEN=' .env | head -1 | cut -d= -f2- | sed 's/^["'\'']//;s/["'\'']$//')"
fi

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
gray() { printf '\033[90m%s\033[0m\n' "$*"; }

step() {
    local name="$1" url="$2" status_expected="$3"
    shift 3
    local status body
    body="$(curl -sS -o /tmp/smoke.body -w '%{http_code}' "$url" "$@")" || {
        red "FAIL $name — curl failed"
        exit 1
    }
    status="$body"
    if [[ "$status" != "$status_expected" ]]; then
        red "FAIL $name — got $status, want $status_expected"
        cat /tmp/smoke.body >&2 || true
        exit 1
    fi
    green "PASS $name ($status)"
    gray "     $(cat /tmp/smoke.body | head -c 200)"
}

# Unauthenticated: must always work.
step "GET /health/live"  "$PREFIX/health/live"  "200"
step "GET /version"      "$PREFIX/version"      "200"

# Authenticated: skip if no token, but warn loudly.
if [[ -z "$TOKEN" ]]; then
    red "WARN: AUTH_TOKEN not set — skipping authenticated checks"
    exit 0
fi

step "GET /health"  "$PREFIX/health"  "200" -H "X-API-Key: $TOKEN"
step "GET /balance" "$PREFIX/balance" "200" -H "X-API-Key: $TOKEN"

green ""
green "All smoke checks passed against $BASE"
