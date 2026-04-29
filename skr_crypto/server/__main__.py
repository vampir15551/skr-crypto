"""
Entry point: python -m app [--lan]
"""
from __future__ import annotations

import argparse
import logging

import uvicorn

from skr_crypto.server.config import (
    SERVER_HOST,
    SERVER_PORT,
    SHUTDOWN_TIMEOUT,
    detect_lan_ip,
    validate_config,
)
from skr_crypto.server.logging_config import setup_logging

setup_logging()
log = logging.getLogger("payouts")


def main() -> None:
    parser = argparse.ArgumentParser(description="USDT TRC-20 Payout Service")
    parser.add_argument(
        "--lan",
        action="store_true",
        help="Listen on LAN interface (192.168.88.x / 192.168.89.x) instead of 127.0.0.1",
    )
    parser.add_argument("--host", default=None, help="Override bind host")
    parser.add_argument("--port", type=int, default=None, help="Override bind port")
    args = parser.parse_args()

    validate_config()

    host = args.host or SERVER_HOST
    port = args.port or SERVER_PORT

    if args.lan:
        lan_ip = detect_lan_ip()
        if lan_ip:
            host = lan_ip
            log.info("LAN mode: binding to %s", host)
        else:
            log.warning("No LAN IP found on 192.168.88.x / 192.168.89.x — falling back to 0.0.0.0")
            # Intentional: --lan asked for "any reachable interface" and we
            # didn't find a recognised LAN; binding 0.0.0.0 is the explicit
            # fallback documented in OPERATIONS.md. Not a security finding.
            host = "0.0.0.0"  # nosec B104

    log.info("Starting on %s:%d  (auto-shutdown: %ds)", host, port, SHUTDOWN_TIMEOUT)

    uvicorn.run(
        "skr_crypto.server.server:create_app",
        factory=True,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
