"""Read-only HTTP client for the underlying service's ``/api/v1`` endpoints.

Usage::

    from skr_crypto.api import APIClient
    client = APIClient.from_env_file(Path("~/.skr-crypto/.env"))
    print(client.health_live())
    print(client.balance())

Design notes:

  - Sync only — matches the service's own constraint and keeps us simple.
  - Hard timeout per request (default 10s). The service's TronGrid round
    trips can take ~50s in the worst case, so for ``/balance`` we bump
    the timeout. Operator visibility ("the API is hanging") is more
    important than waiting forever.
  - Auth header is always set when AUTH_TOKEN is known. Endpoints that
    don't need it (``/health/live``, ``/version``) ignore it.
  - We never call ``/send``. There is no helper for it; adding one
    would defeat the whole point of this CLI being read-only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from skr_crypto.config import read_env_file
from skr_crypto.exceptions import (
    AuthError,
    BadResponseError,
    ServiceUnreachableError,
)


class APIClient:
    """Thin wrapper around requests that does the auth + error mapping."""

    def __init__(
        self,
        base_url: str,
        auth_token: str | None,
        *,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._timeout = timeout
        self._session = requests.Session()

    # -- factories --------------------------------------------------------

    @classmethod
    def from_env_file(cls, env_path: Path, **kwargs: Any) -> APIClient:
        """Read AUTH_TOKEN + SERVER_HOST + SERVER_PORT from the service's
        .env. Raises ConfigError if the file is missing."""
        env = read_env_file(env_path)
        host = env.get("SERVER_HOST", "127.0.0.1")
        port = env.get("SERVER_PORT", "8000")
        # If the operator bound to 0.0.0.0 we still hit it on localhost —
        # the CLI runs alongside the service in our typical install.
        if host in ("0.0.0.0", ""):
            host = "127.0.0.1"
        url = f"http://{host}:{port}"
        return cls(url, env.get("AUTH_TOKEN") or None, **kwargs)

    # -- low-level --------------------------------------------------------

    def _get(self, path: str, *, auth: bool = True, timeout: float | None = None) -> Any:
        headers: dict[str, str] = {}
        if auth and self._auth_token:
            headers["X-API-Key"] = self._auth_token
        url = f"{self.base_url}{path}"
        try:
            r = self._session.get(url, headers=headers, timeout=timeout or self._timeout)
        except requests.ConnectionError as exc:
            raise ServiceUnreachableError(
                f"Cannot reach {url}: {exc}. Is the service running? "
                f"Try `skr-crypto status`."
            ) from exc
        except requests.Timeout as exc:
            raise ServiceUnreachableError(
                f"Timed out talking to {url}: {exc}."
            ) from exc

        if r.status_code == 401:
            raise AuthError(
                "API rejected our X-API-Key. The token in .env may be stale; "
                "regenerate via `skr-crypto config edit` and restart the service."
            )
        if r.status_code >= 500:
            raise BadResponseError(
                f"{path} returned {r.status_code}: {r.text[:300]}"
            )
        if not r.ok:
            raise BadResponseError(
                f"{path} returned {r.status_code}: {r.text[:300]}"
            )
        try:
            return r.json()
        except ValueError as exc:
            raise BadResponseError(
                f"{path} returned non-JSON body: {r.text[:300]}"
            ) from exc

    # -- endpoints --------------------------------------------------------

    def health_live(self) -> dict[str, Any]:
        return self._get("/api/v1/health/live", auth=False)

    def version(self) -> dict[str, Any]:
        return self._get("/api/v1/version", auth=False)

    def health(self) -> dict[str, Any]:
        return self._get("/api/v1/health")

    def balance(self) -> dict[str, Any]:
        # Balance hits TronGrid through the service — generous timeout.
        return self._get("/api/v1/balance", timeout=30.0)

    def metrics(self) -> str:
        # /metrics returns Prometheus text, not JSON.
        url = f"{self.base_url}/api/v1/metrics"
        headers = {"X-API-Key": self._auth_token} if self._auth_token else {}
        try:
            r = self._session.get(url, headers=headers, timeout=self._timeout)
        except requests.RequestException as exc:
            raise ServiceUnreachableError(str(exc)) from exc
        if r.status_code == 401:
            raise AuthError("API rejected our X-API-Key for /metrics.")
        if not r.ok:
            raise BadResponseError(f"/metrics returned {r.status_code}")
        return r.text
