"""Pluggable secret backends for the TRON treasury private key.

The service supports four backends, selected via ``KEY_PROVIDER``:

  - ``1password`` — the historical default. Uses the 1Password CLI (``op``).
    Key never lives on disk; requires biometric/touch unlock at start.
    Best for an operator-on-laptop scenario.
  - ``env`` — read raw hex from the ``PRIVATE_KEY_HEX`` env var.
    Simplest backend; key lives in the environment of the process.
    Best for containers / orchestrators that already inject secrets.
  - ``file`` — read raw hex from ``PRIVATE_KEY_FILE``.
    File must be ``chmod 600``; we refuse to load otherwise.
    Best for plain VPS deployments behind systemd.
  - ``keychain`` — read from the macOS Keychain (``security find-generic-password``).
    Service: ``KEYCHAIN_SERVICE`` (default "payouts"), account:
    ``KEYCHAIN_ACCOUNT`` (default "treasury").
    Best for local dev on a mac without 1Password.

All providers return a 32-byte ``bytearray`` so the caller can ``wipe_bytearray``
once the key is loaded into the signing object. See ``security.py`` for a
realistic disclosure of what wiping bytes in CPython actually buys you.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Protocol

from skr_crypto.server.config import (
    KEY_PROVIDER,
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE,
    OP_FIELD,
    OP_ITEM,
    OP_VAULT,
    PRIVATE_KEY_FILE,
)

log = logging.getLogger("payouts")


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _wipe(buf: bytearray) -> None:
    """Overwrite a bytearray with zeros."""
    for i in range(len(buf)):
        buf[i] = 0


def _decode_hex_to_bytes(raw: bytearray, source_label: str) -> bytearray:
    """Strip whitespace + 0x prefix, hex-decode, validate 32-byte length.

    The intermediate ``raw`` bytearray is wiped before raising / returning
    so we don't leave a hex copy of the key on the heap. Errors do not
    include any portion of the key bytes — even a prefix could leak.
    """
    raw = bytearray(raw).rstrip(b"\r\n").rstrip()
    if not raw:
        log.error("%s returned an empty private key", source_label)
        sys.exit(1)
    if len(raw) >= 2 and raw[:2] in (b"0x", b"0X"):
        del raw[:2]
    try:
        key_bytes = bytearray.fromhex(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        log.error("Private key from %s is not valid hex", source_label)
        _wipe(raw)
        sys.exit(1)
    _wipe(raw)
    if len(key_bytes) != 32:
        log.error(
            "Private key from %s has wrong length: %d bytes (expected 32)",
            source_label, len(key_bytes),
        )
        _wipe(key_bytes)
        sys.exit(1)
    return key_bytes


# ---------------------------------------------------------------------------
# Provider protocol + implementations
# ---------------------------------------------------------------------------


class KeyProvider(Protocol):
    """Loads a TRON private key and optionally locks the underlying secret store.

    Implementations must:
      - Return a 32-byte ``bytearray`` from ``get_private_key``.
      - Never log the key (or any prefix of it).
      - ``lock`` is best-effort and called at process exit.
    """

    def get_private_key(self) -> bytearray: ...

    def lock(self) -> None: ...


class OnePasswordKeyProvider:
    """Read the key from the 1Password CLI.

    Requires ``op`` on PATH and an unlocked session (``op signin``).
    Uses ``--reveal`` and reads stdout as raw bytes (``text=False``) to
    avoid CPython codec/intern buffers leaving copies on the heap.
    """

    def get_private_key(self) -> bytearray:
        try:
            result = subprocess.run(
                ["op", "item", "get", OP_ITEM, "--vault", OP_VAULT,
                 "--fields", OP_FIELD, "--reveal"],
                capture_output=True,
                text=False,
                timeout=30,
            )
        except FileNotFoundError:
            log.error("1Password CLI (op) not found")
            sys.exit(1)
        except subprocess.TimeoutExpired:
            log.error("1Password CLI timed out — run: op signin")
            sys.exit(1)

        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            log.error("Failed to read key from 1Password: %s", stderr)
            sys.exit(1)

        key = _decode_hex_to_bytes(bytearray(result.stdout or b""), "1Password")

        # Best-effort: drop subprocess-side references early so GC can reclaim
        # them; these are immutable bytes so this is no-op semantically.
        try:
            result.stdout = b""
            result.stderr = b""
        except Exception:
            pass

        return key

    def lock(self) -> None:
        try:
            subprocess.run(["op", "signout", "--all"], capture_output=True, timeout=10)
            log.info("1Password locked")
        except Exception as exc:
            log.warning("Could not lock 1Password: %s", exc)


class EnvKeyProvider:
    """Read raw hex from the ``PRIVATE_KEY_HEX`` env var.

    Note: the variable lives in the process environment for the entire
    process lifetime — anything inside the process can read it via
    ``os.environ``. This is the simplest backend; suitable for containers
    where the orchestrator injects the secret and rotates it via redeploy.
    """

    _ENV = "PRIVATE_KEY_HEX"

    def get_private_key(self) -> bytearray:
        raw_str = os.environ.get(self._ENV, "")
        if not raw_str:
            log.error("KEY_PROVIDER=env requires %s to be set", self._ENV)
            sys.exit(1)
        # We have no way to wipe the str CPython internalised — best we can
        # do is encode to bytes (mutable) and clear the env var ASAP.
        raw = bytearray(raw_str.encode("ascii"))
        try:
            del os.environ[self._ENV]
        except KeyError:
            pass
        return _decode_hex_to_bytes(raw, "PRIVATE_KEY_HEX")

    def lock(self) -> None:
        # No backend to lock; the env var is already deleted.
        return None


class FileKeyProvider:
    """Read raw hex from ``PRIVATE_KEY_FILE``.

    Refuses to load if the file is world- or group-readable (any mode bit
    other than 0600 on the user). Hard requirement: a casual ``ls`` from
    another user must not be able to dump the key.
    """

    def get_private_key(self) -> bytearray:
        if not PRIVATE_KEY_FILE:
            log.error("KEY_PROVIDER=file requires PRIVATE_KEY_FILE to be set")
            sys.exit(1)
        try:
            st = os.stat(PRIVATE_KEY_FILE)
        except FileNotFoundError:
            log.error("PRIVATE_KEY_FILE not found: %s", PRIVATE_KEY_FILE)
            sys.exit(1)
        # Reject if group/other have any access bits set. We don't check
        # ownership — the user running the service might legitimately read
        # a file they don't own (uncommon but valid).
        if st.st_mode & 0o077:
            log.error(
                "PRIVATE_KEY_FILE %s has unsafe mode %#o — set chmod 600 "
                "(only the owner may read/write)",
                PRIVATE_KEY_FILE, st.st_mode & 0o777,
            )
            sys.exit(1)
        try:
            with open(PRIVATE_KEY_FILE, "rb") as fp:
                raw = bytearray(fp.read())
        except OSError as exc:
            log.error("Failed to read PRIVATE_KEY_FILE %s: %s",
                      PRIVATE_KEY_FILE, exc)
            sys.exit(1)
        return _decode_hex_to_bytes(raw, f"file {PRIVATE_KEY_FILE}")

    def lock(self) -> None:
        return None


class KeychainKeyProvider:
    """Read raw hex from the macOS Keychain.

    Uses ``security find-generic-password -s <service> -a <account> -w``,
    which prints the plaintext to stdout. Requires the user to have stored
    the key with::

        security add-generic-password \\
            -s payouts -a treasury -w '<hex-private-key>' -U
    """

    def get_private_key(self) -> bytearray:
        if sys.platform != "darwin":
            log.error("KEY_PROVIDER=keychain only works on macOS")
            sys.exit(1)
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", KEYCHAIN_SERVICE,
                 "-a", KEYCHAIN_ACCOUNT,
                 "-w"],
                capture_output=True,
                text=False,
                timeout=15,
            )
        except FileNotFoundError:
            log.error("macOS 'security' tool not found — cannot use keychain provider")
            sys.exit(1)

        if result.returncode != 0:
            stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            log.error(
                "Keychain lookup failed (service=%s account=%s): %s",
                KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, stderr,
            )
            sys.exit(1)

        return _decode_hex_to_bytes(
            bytearray(result.stdout or b""),
            f"keychain {KEYCHAIN_SERVICE}/{KEYCHAIN_ACCOUNT}",
        )

    def lock(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Registry / factory
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, type[KeyProvider]] = {
    "1password": OnePasswordKeyProvider,
    "env": EnvKeyProvider,
    "file": FileKeyProvider,
    "keychain": KeychainKeyProvider,
}


def get_key_provider() -> KeyProvider:
    """Return the configured KeyProvider instance.

    Selection is based on the ``KEY_PROVIDER`` config value (already
    validated at startup; see ``app.config.validate_config``).
    """
    cls = _REGISTRY.get(KEY_PROVIDER)
    if cls is None:
        log.error(
            "Unknown KEY_PROVIDER=%r (valid: %s)",
            KEY_PROVIDER, ", ".join(sorted(_REGISTRY)),
        )
        sys.exit(1)
    log.info("Key provider: %s", KEY_PROVIDER)
    return cls()


def supported_providers() -> tuple[str, ...]:
    """For setup wizard / docs."""
    return tuple(sorted(_REGISTRY))
