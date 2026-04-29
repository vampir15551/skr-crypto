"""Network helpers — client IP resolution with X-Forwarded-For support."""
from __future__ import annotations

from fastapi import Request

from skr_crypto.server.config import TRUSTED_PROXIES


def client_address(request: Request) -> str:
    """Return the originating client IP for logging / rate limiting.

    By default we trust ONLY the direct peer (request.client.host) — XFF
    headers from clients can be trivially spoofed.

    If the direct peer is in TRUSTED_PROXIES (configured via env), we read
    the *first* hop from X-Forwarded-For: the leftmost address is the
    original client per RFC 7239 / standard proxy convention.

    Returns "unknown" only if the peer is missing entirely (extremely
    unusual — would indicate a misconfigured ASGI server).
    """
    direct = request.client.host if request.client else "unknown"
    if direct == "unknown" or not TRUSTED_PROXIES:
        return direct
    if direct not in TRUSTED_PROXIES:
        return direct
    xff = request.headers.get("X-Forwarded-For", "").strip()
    if not xff:
        return direct
    # XFF may be "client, proxy1, proxy2" — take the leftmost (original client).
    first = xff.split(",", 1)[0].strip()
    return first or direct
