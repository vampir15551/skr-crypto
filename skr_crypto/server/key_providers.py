"""Pluggable secret backends for TRON treasury private keys.

Five backends, selected via ``KEY_PROVIDER``:

  - ``1password``      — 1Password CLI (``op``). Operator-on-laptop default.
  - ``env``            — raw hex in env vars. Container / orchestrator-friendly.
  - ``file``           — raw hex in chmod-600 file(s). Plain-VPS friendly.
  - ``keychain``       — macOS Keychain. Local dev on a mac without 1Password.
  - ``encrypted_file`` — single AES-256-GCM + scrypt JSON file holding any
                         number of keys behind one passphrase. Works in
                         containers without 1Password / Keychain.

All providers expose ``load_wallets() -> list[LoadedWallet]``. ``LoadedWallet``
carries the wallet's name, an advertised address (best-effort, may be ""),
and a 32-byte ``bytearray`` containing the raw private key. The caller is
responsible for ``wipe`` ing each ``raw_key`` once it's been loaded into a
``tronpy.PrivateKey`` (``security.load_wallets`` does this).

Multi-wallet config:

  - **encrypted_file** is multi-wallet by construction — the file is a JSON
    array of encrypted entries.
  - For ``env`` / ``file`` / ``1password`` / ``keychain``, multi-wallet is
    opt-in via ``WALLETS=name1,name2,name3``. For each name, the provider
    reads a per-name env var or per-name address into the secret store:

        env:        WALLET_<NAME>_PRIVATE_KEY_HEX    (uppercased)
        file:       WALLET_<NAME>_PRIVATE_KEY_FILE
        1password:  WALLET_<NAME>_OP_ITEM            (vault from OP_VAULT)
        keychain:   WALLET_<NAME>_KEYCHAIN_ACCOUNT   (service from KEYCHAIN_SERVICE)

  - **Legacy single-wallet** (no ``WALLETS`` env var) keeps working
    unchanged. The legacy globals (``PRIVATE_KEY_HEX``, ``PRIVATE_KEY_FILE``,
    ``OP_ITEM``, ``KEYCHAIN_ACCOUNT``) are loaded as a single wallet named
    ``"default"``.

Wiping caveats are documented in ``security.py`` — short version: the
``bytearray`` is zeroed, but any prior CPython interning is out of our
hands, so we minimise hex→str round-trips wherever possible.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Protocol

from skr_crypto.server.config import (
    KEY_PASSPHRASE_FILE,
    KEY_PROVIDER,
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE,
    KEYSTORE_FILE,
    OP_FIELD,
    OP_ITEM,
    OP_VAULT,
    PRIVATE_KEY_FILE,
    WALLETS,
)

log = logging.getLogger("payouts")

DEFAULT_WALLET_NAME = "default"


# ---------------------------------------------------------------------------
# Common types
# ---------------------------------------------------------------------------


@dataclass
class LoadedWallet:
    """Provider output. ``raw_key`` is a 32-byte mutable bytearray that
    the caller must wipe after constructing a tronpy ``PrivateKey``.

    ``address`` is a best-effort hint from the provider (e.g. the address
    field stored alongside the encrypted key). It is NOT authoritative —
    the caller derives the canonical address from the private key and
    cross-checks. If the provider has no hint, it leaves this empty.
    """
    name: str
    raw_key: bytearray
    address: str = ""


class KeyProvider(Protocol):
    """Loads zero-or-more TRON private keys."""

    def load_wallets(self) -> list[LoadedWallet]: ...

    def lock(self) -> None: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wipe(buf: bytearray) -> None:
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


def _wallet_names_from_config() -> list[str]:
    """Parse the ``WALLETS`` env var into a list of valid wallet names.

    Returns [] if the env var is unset/empty (signals "use legacy
    single-wallet config"). Names must be ASCII identifiers — we use them
    in env var suffixes and JSON keys, both of which intersect badly with
    fancy unicode."""
    raw = (WALLETS or "").strip()
    if not raw:
        return []
    names: list[str] = []
    for part in raw.split(","):
        n = part.strip()
        if not n:
            continue
        if not n.replace("_", "").replace("-", "").isalnum():
            log.error(
                "Invalid wallet name %r in WALLETS — use letters/digits/_/- only",
                n,
            )
            sys.exit(1)
        if n in names:
            log.error("Duplicate wallet name %r in WALLETS", n)
            sys.exit(1)
        names.append(n)
    return names


def _env_var_for(name: str, suffix: str) -> str:
    """Compose ``WALLET_<NAME>_<SUFFIX>`` with name uppercased.

    Hyphens map to underscores so ``main-treasury`` becomes
    ``WALLET_MAIN_TREASURY_PRIVATE_KEY_HEX``."""
    safe = name.upper().replace("-", "_")
    return f"WALLET_{safe}_{suffix}"


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


class OnePasswordKeyProvider:
    """Read keys from the 1Password CLI.

    Multi-wallet: if ``WALLETS`` is set, each wallet's item name comes
    from ``WALLET_<NAME>_OP_ITEM``. Single vault for everything (most
    teams keep treasury keys together)."""

    def load_wallets(self) -> list[LoadedWallet]:
        names = _wallet_names_from_config()
        if not names:
            return [self._fetch(DEFAULT_WALLET_NAME, OP_ITEM)]
        out: list[LoadedWallet] = []
        for n in names:
            item = os.environ.get(_env_var_for(n, "OP_ITEM"), "")
            if not item:
                log.error(
                    "KEY_PROVIDER=1password: %s not set "
                    "(needed because WALLETS includes %r)",
                    _env_var_for(n, "OP_ITEM"), n,
                )
                sys.exit(1)
            out.append(self._fetch(n, item))
        return out

    def _fetch(self, wallet_name: str, op_item: str) -> LoadedWallet:
        try:
            result = subprocess.run(
                ["op", "item", "get", op_item, "--vault", OP_VAULT,
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
            log.error(
                "Failed to read key %r (item=%s) from 1Password: %s",
                wallet_name, op_item, stderr,
            )
            sys.exit(1)

        key = _decode_hex_to_bytes(
            bytearray(result.stdout or b""),
            f"1Password item {op_item!r}",
        )

        try:
            result.stdout = b""
            result.stderr = b""
        except Exception:
            pass

        return LoadedWallet(name=wallet_name, raw_key=key)

    def lock(self) -> None:
        try:
            subprocess.run(["op", "signout", "--all"], capture_output=True, timeout=10)
            log.info("1Password locked")
        except Exception as exc:
            log.warning("Could not lock 1Password: %s", exc)


class EnvKeyProvider:
    """Read raw hex from env vars.

    Single-wallet legacy: ``PRIVATE_KEY_HEX``.
    Multi-wallet: ``WALLET_<NAME>_PRIVATE_KEY_HEX`` for each name in WALLETS.

    Env vars are deleted from ``os.environ`` after read (best-effort —
    the str CPython internalised could still linger; see security.py)."""

    _LEGACY_ENV = "PRIVATE_KEY_HEX"

    def load_wallets(self) -> list[LoadedWallet]:
        names = _wallet_names_from_config()
        if not names:
            return [self._fetch_one(DEFAULT_WALLET_NAME, self._LEGACY_ENV)]
        out: list[LoadedWallet] = []
        for n in names:
            out.append(self._fetch_one(n, _env_var_for(n, "PRIVATE_KEY_HEX")))
        return out

    def _fetch_one(self, wallet_name: str, env_var: str) -> LoadedWallet:
        raw_str = os.environ.get(env_var, "")
        if not raw_str:
            log.error(
                "KEY_PROVIDER=env: %s is not set (wallet=%r)",
                env_var, wallet_name,
            )
            sys.exit(1)
        raw = bytearray(raw_str.encode("ascii"))
        try:
            del os.environ[env_var]
        except KeyError:
            pass
        key = _decode_hex_to_bytes(raw, env_var)
        return LoadedWallet(name=wallet_name, raw_key=key)

    def lock(self) -> None:
        return None


class FileKeyProvider:
    """Read raw hex from chmod-600 file(s).

    Single-wallet legacy: ``PRIVATE_KEY_FILE``.
    Multi-wallet: ``WALLET_<NAME>_PRIVATE_KEY_FILE`` for each name."""

    def load_wallets(self) -> list[LoadedWallet]:
        names = _wallet_names_from_config()
        if not names:
            if not PRIVATE_KEY_FILE:
                log.error("KEY_PROVIDER=file requires PRIVATE_KEY_FILE to be set")
                sys.exit(1)
            return [self._read_file(DEFAULT_WALLET_NAME, PRIVATE_KEY_FILE)]
        out: list[LoadedWallet] = []
        for n in names:
            path = os.environ.get(_env_var_for(n, "PRIVATE_KEY_FILE"), "")
            if not path:
                log.error(
                    "KEY_PROVIDER=file: %s is not set (wallet=%r)",
                    _env_var_for(n, "PRIVATE_KEY_FILE"), n,
                )
                sys.exit(1)
            out.append(self._read_file(n, path))
        return out

    def _read_file(self, wallet_name: str, path: str) -> LoadedWallet:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            log.error("PRIVATE_KEY_FILE not found: %s (wallet=%r)", path, wallet_name)
            sys.exit(1)
        if st.st_mode & 0o077:
            log.error(
                "PRIVATE_KEY_FILE %s has unsafe mode %#o — set chmod 600 "
                "(only the owner may read/write) (wallet=%r)",
                path, st.st_mode & 0o777, wallet_name,
            )
            sys.exit(1)
        try:
            with open(path, "rb") as fp:
                raw = bytearray(fp.read())
        except OSError as exc:
            log.error(
                "Failed to read PRIVATE_KEY_FILE %s: %s (wallet=%r)",
                path, exc, wallet_name,
            )
            sys.exit(1)
        return LoadedWallet(
            name=wallet_name,
            raw_key=_decode_hex_to_bytes(raw, f"file {path}"),
        )

    def lock(self) -> None:
        return None


class KeychainKeyProvider:
    """Read raw hex from the macOS Keychain.

    Single-wallet legacy: ``KEYCHAIN_SERVICE`` + ``KEYCHAIN_ACCOUNT``.
    Multi-wallet: same service for all, per-wallet ``WALLET_<NAME>_KEYCHAIN_ACCOUNT``.

    Stored with::

        security add-generic-password -s payouts -a <ACCOUNT> -w '<hex>' -U
    """

    def load_wallets(self) -> list[LoadedWallet]:
        if sys.platform != "darwin":
            log.error("KEY_PROVIDER=keychain only works on macOS")
            sys.exit(1)
        names = _wallet_names_from_config()
        if not names:
            return [self._read_one(DEFAULT_WALLET_NAME, KEYCHAIN_ACCOUNT)]
        out: list[LoadedWallet] = []
        for n in names:
            account = os.environ.get(_env_var_for(n, "KEYCHAIN_ACCOUNT"), "")
            if not account:
                log.error(
                    "KEY_PROVIDER=keychain: %s is not set (wallet=%r)",
                    _env_var_for(n, "KEYCHAIN_ACCOUNT"), n,
                )
                sys.exit(1)
            out.append(self._read_one(n, account))
        return out

    def _read_one(self, wallet_name: str, account: str) -> LoadedWallet:
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", KEYCHAIN_SERVICE,
                 "-a", account,
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
                "Keychain lookup failed (wallet=%r service=%s account=%s): %s",
                wallet_name, KEYCHAIN_SERVICE, account, stderr,
            )
            sys.exit(1)
        return LoadedWallet(
            name=wallet_name,
            raw_key=_decode_hex_to_bytes(
                bytearray(result.stdout or b""),
                f"keychain {KEYCHAIN_SERVICE}/{account}",
            ),
        )

    def lock(self) -> None:
        return None


class EncryptedFileKeyProvider:
    """Read keys from a single encrypted JSON keystore.

    Multi-wallet by construction: the keystore file is a JSON array of
    AES-256-GCM blobs sharing one scrypt-derived key. ``WALLETS`` is
    ignored — the file is the source of truth.

    Passphrase resolution (priority order, see encrypted_keystore.resolve_passphrase):

      1. ``KEY_PASSPHRASE`` env var (consumed and deleted after use).
      2. ``KEY_PASSPHRASE_FILE`` chmod-600 file.
      3. Interactive ``getpass`` prompt — only if stdin is a TTY.

    Containers should set ``KEY_PASSPHRASE`` via Docker secrets / k8s
    Secret env. Plain VPS deploys can use ``KEY_PASSPHRASE_FILE`` so the
    passphrase isn't visible in ``ps``."""

    def load_wallets(self) -> list[LoadedWallet]:
        # Lazy import: only this provider needs cryptography.
        from skr_crypto.server.encrypted_keystore import (
            KeystoreError,
            KeystoreNotFound,
            WrongPassphrase,
            load_keystore,
            resolve_passphrase,
        )

        if not KEYSTORE_FILE:
            log.error("KEY_PROVIDER=encrypted_file requires KEYSTORE_FILE to be set")
            sys.exit(1)

        passphrase_env = os.environ.get("KEY_PASSPHRASE", "")
        try:
            passphrase = resolve_passphrase(
                env_var_value=passphrase_env or None,
                file_path=KEY_PASSPHRASE_FILE or None,
                allow_tty_prompt=True,
            )
        except Exception as exc:
            log.error("Failed to resolve keystore passphrase: %s", exc)
            sys.exit(1)

        # Drop the env var ASAP. The decoded bytes still live in
        # ``passphrase`` (mutable bytes), but at least the environment
        # is clean for any subprocesses we spawn.
        if passphrase_env:
            try:
                del os.environ["KEY_PASSPHRASE"]
            except KeyError:
                pass

        try:
            entries = load_keystore(KEYSTORE_FILE, passphrase)
        except KeystoreNotFound:
            log.error("Keystore file not found: %s", KEYSTORE_FILE)
            sys.exit(1)
        except WrongPassphrase:
            log.error("Keystore passphrase rejected — check KEY_PASSPHRASE")
            sys.exit(1)
        except KeystoreError as exc:
            log.error("Keystore load failed: %s", exc)
            sys.exit(1)

        if not entries:
            log.error(
                "Keystore %s contains zero wallets — add one with "
                "`skr-crypto wallet add` before starting the server",
                KEYSTORE_FILE,
            )
            sys.exit(1)

        return [
            LoadedWallet(name=e.name, raw_key=e.raw_key, address=e.address)
            for e in entries
        ]

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
    "encrypted_file": EncryptedFileKeyProvider,
}


def get_key_provider() -> KeyProvider:
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
