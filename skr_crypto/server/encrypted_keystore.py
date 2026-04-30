"""Encrypted on-disk keystore for multi-wallet treasuries.

Goal: a single chmod-600 JSON file that can hold any number of TRON
private keys, encrypted at rest with a passphrase. Designed to give
container deploys (Docker, k8s, plain VPS-without-1Password) the same
"keys never lie around in plaintext" guarantee that 1Password gives
laptop operators.

Format (``version: 1``):

.. code-block:: json

    {
      "version": 1,
      "kdf": "scrypt",
      "kdf_params": {"n": 32768, "r": 8, "p": 1, "salt_b64": "..."},
      "cipher": "aes-256-gcm",
      "wallets": [
        {
          "name": "main",
          "address": "TXxx...",
          "iv_b64": "...",
          "ciphertext_b64": "..."
        }
      ]
    }

  - **One passphrase, one derived key** for the whole file (scrypt with
    salt stored alongside). Adding a wallet only requires the passphrase.
  - **Per-wallet IV** so two wallets with the same key (silly but possible)
    don't produce identical ciphertext.
  - **AAD = wallet name**, so swapping ciphertext blobs between wallet
    entries inside the same file fails authentication.
  - **Plaintext = 32 raw private-key bytes** (not hex). Hex would mean
    decoding back into a bytearray on every load with extra copies on
    the heap; raw bytes round-trip cleanly.

Why scrypt over PBKDF2 / argon2:

  - scrypt is in ``cryptography.hazmat.primitives.kdf.scrypt``, no extra
    dependency, well-reviewed, memory-hard. argon2id would be marginally
    nicer but adds ``argon2-cffi`` for a passphrase that's typed once
    per process.
  - Defaults (n=32768, r=8, p=1) cost ~30ms on a modern Mac and ~32MB
    of RAM — enough to make GPU brute-force expensive without tipping
    over a constrained container.

Why AES-256-GCM over ChaCha20-Poly1305:

  - AES-NI everywhere we'd realistically run; performance equivalent.
  - The Python ``cryptography`` ``AESGCM`` API is the simplest one in
    the library — single call to encrypt/decrypt, with built-in tag
    handling.

Honest limitations:

  - The passphrase lives in CPython memory while we derive the key.
    Same caveats as ``security.py`` — we can't reliably wipe Python
    ``str`` objects. The keystore minimises exposure (passphrase
    bytes consumed once, scrypt result reused for the lifetime of the
    process) but doesn't pretend to defeat a memory-dumping adversary.
  - The on-disk file is the only copy. Lose it and the passphrase
    and you've lost the keys forever. Back it up alongside whatever
    ``IDEMPOTENCY_DB_PATH`` and ``AUDIT_LOG_FILE`` you back up.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

log = logging.getLogger("payouts")


# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

CURRENT_VERSION: int = 1

# scrypt parameters. Bump only via a versioned migration.
SCRYPT_N: int = 32768
SCRYPT_R: int = 8
SCRYPT_P: int = 1
SCRYPT_KEYLEN: int = 32  # 256-bit AES key
SCRYPT_SALT_BYTES: int = 16  # 128 bits, ample
AES_GCM_IV_BYTES: int = 12  # NIST-recommended length for GCM


# ---------------------------------------------------------------------------
# Errors — distinguished so callers can give precise UX.
# ---------------------------------------------------------------------------


class KeystoreError(Exception):
    """Base class for keystore problems."""


class KeystoreNotFound(KeystoreError):
    """Path doesn't exist."""


class KeystoreCorrupt(KeystoreError):
    """JSON malformed or missing required fields. Distinct from a wrong
    passphrase — we want to tell the operator their file is broken vs.
    their passphrase is wrong."""


class KeystoreUnsupportedVersion(KeystoreError):
    pass


class WrongPassphrase(KeystoreError):
    """Passphrase decryption failed (GCM tag mismatch)."""


class WalletNameConflict(KeystoreError):
    """Refusing to add a wallet with a name that already exists."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class KeystoreEntry:
    """Plaintext-equivalent record of one wallet inside the keystore.

    ``raw_key`` is a 32-byte ``bytearray`` (mutable so callers can wipe
    it after loading into a tronpy ``PrivateKey``).
    """
    name: str
    address: str
    raw_key: bytearray


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def _derive_key(passphrase: bytes, salt: bytes) -> bytes:
    """scrypt(passphrase, salt) → 32-byte AES key.

    The ``Scrypt`` object can only do one ``derive`` call; we burn one
    instance per call.
    """
    kdf = Scrypt(
        salt=salt, length=SCRYPT_KEYLEN, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
    )
    return kdf.derive(passphrase)


def _validate_priv_bytes(b: bytes | bytearray, source_label: str) -> None:
    """Refuse to encrypt anything other than a 32-byte non-zero key."""
    if len(b) != 32:
        raise KeystoreError(
            f"{source_label}: expected 32 raw private-key bytes, got {len(b)}"
        )
    if all(byte == 0 for byte in b):
        raise KeystoreError(
            f"{source_label}: private key is all zeroes — refusing to encrypt"
        )


def _check_file_mode(path: Path) -> None:
    """Refuse to load a keystore that's group/other readable.

    Mirrors ``FileKeyProvider``: a casual ``ls -la`` from an unprivileged
    user must not be enough to grab the ciphertext.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        raise KeystoreNotFound(str(path))
    if st.st_mode & 0o077:
        raise KeystoreError(
            f"keystore {path} has unsafe mode {stat.S_IMODE(st.st_mode):#o} — "
            f"chmod 600 the file (only the owner may read/write)"
        )


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


def load_keystore(
    path: str | Path, passphrase: bytes,
) -> list[KeystoreEntry]:
    """Decrypt every wallet in the file. Returns them in stored order.

    Raises:
        KeystoreNotFound, KeystoreCorrupt, KeystoreUnsupportedVersion,
        WrongPassphrase, KeystoreError (mode-bits / general).
    """
    p = Path(path)
    _check_file_mode(p)

    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise KeystoreError(f"read {p}: {exc}")

    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise KeystoreCorrupt(f"keystore {p} is not valid JSON: {exc}")

    if not isinstance(doc, dict):
        raise KeystoreCorrupt(f"keystore {p} root is not an object")

    version = doc.get("version")
    if version != CURRENT_VERSION:
        raise KeystoreUnsupportedVersion(
            f"keystore {p} version={version!r}, this binary only handles "
            f"version={CURRENT_VERSION}"
        )

    if doc.get("kdf") != "scrypt" or doc.get("cipher") != "aes-256-gcm":
        raise KeystoreCorrupt(
            f"keystore {p} uses unexpected kdf/cipher combination: "
            f"kdf={doc.get('kdf')!r} cipher={doc.get('cipher')!r}"
        )

    kdf_params = doc.get("kdf_params") or {}
    try:
        salt = _b64d(kdf_params["salt_b64"])
    except (KeyError, ValueError) as exc:
        raise KeystoreCorrupt(f"keystore {p} salt missing/invalid: {exc}")

    # Re-derive once for the whole file. scrypt is the slow part of every
    # keystore operation; doing it per-wallet would multiply boot time.
    try:
        aes_key = _derive_key(passphrase, salt)
    except Exception as exc:
        raise KeystoreError(f"scrypt derive failed: {exc}")
    aead = AESGCM(aes_key)

    wallets_doc = doc.get("wallets")
    if not isinstance(wallets_doc, list):
        raise KeystoreCorrupt(f"keystore {p} 'wallets' is not a list")

    out: list[KeystoreEntry] = []
    seen_names: set[str] = set()
    for i, w in enumerate(wallets_doc):
        if not isinstance(w, dict):
            raise KeystoreCorrupt(f"keystore {p} wallet[{i}] is not an object")
        name = w.get("name")
        if not isinstance(name, str) or not name:
            raise KeystoreCorrupt(
                f"keystore {p} wallet[{i}] missing/empty 'name'"
            )
        if name in seen_names:
            raise KeystoreCorrupt(
                f"keystore {p} has duplicate wallet name: {name!r}"
            )
        seen_names.add(name)

        try:
            iv = _b64d(w["iv_b64"])
            ct = _b64d(w["ciphertext_b64"])
        except (KeyError, ValueError) as exc:
            raise KeystoreCorrupt(
                f"keystore {p} wallet[{i}] iv/ciphertext invalid: {exc}"
            )

        try:
            plain = aead.decrypt(iv, ct, name.encode("utf-8"))
        except InvalidTag:
            # Wrong passphrase OR tampered file. We can't tell — same
            # error on the wire.
            raise WrongPassphrase(
                f"keystore {p}: passphrase rejected (wallet={name!r})"
            )
        try:
            _validate_priv_bytes(plain, f"keystore {p} wallet[{i}]")
        except KeystoreError:
            # Don't leak even partial bytes; just re-raise.
            raise

        address = w.get("address", "")
        if not isinstance(address, str):
            address = ""
        out.append(KeystoreEntry(
            name=name,
            address=address,
            raw_key=bytearray(plain),
        ))

    return out


def list_wallet_names(path: str | Path) -> list[str]:
    """Read wallet names + advertised addresses without decrypting.

    Useful for ``skr-crypto wallet list`` when the operator hasn't
    typed the passphrase yet.
    """
    p = Path(path)
    _check_file_mode(p)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KeystoreCorrupt(f"read {p}: {exc}")
    wallets = doc.get("wallets") or []
    return [w.get("name", "") for w in wallets if isinstance(w, dict)]


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------


def init_keystore(
    path: str | Path,
    passphrase: bytes,
    *,
    overwrite: bool = False,
) -> None:
    """Create an empty (zero-wallet) keystore at ``path``.

    Idempotency: refuses to overwrite unless ``overwrite=True``. The
    file is created with mode 0600 atomically (O_CREAT | O_EXCL).
    """
    p = Path(path)
    if p.exists() and not overwrite:
        raise KeystoreError(
            f"keystore {p} already exists — pass overwrite=True or remove it"
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    # Validate the passphrase derives — catches an empty bytes input
    # before we write anything.
    if not passphrase:
        raise KeystoreError("passphrase is empty")
    _ = _derive_key(passphrase, salt)
    doc = {
        "version": CURRENT_VERSION,
        "kdf": "scrypt",
        "kdf_params": {
            "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt_b64": _b64e(salt),
        },
        "cipher": "aes-256-gcm",
        "wallets": [],
    }
    _atomic_write(p, doc, overwrite=overwrite)


def save_keystore(
    path: str | Path,
    passphrase: bytes,
    entries: list[KeystoreEntry],
    *,
    salt: bytes | None = None,
) -> None:
    """Re-encrypt ``entries`` and write atomically.

    If ``salt`` is None we generate a fresh one (passphrase rotation),
    otherwise we reuse — caller passes the salt from a prior load when
    they're just adding/removing entries without changing the passphrase.
    """
    p = Path(path)
    if salt is None:
        salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    aes_key = _derive_key(passphrase, salt)
    aead = AESGCM(aes_key)

    seen: set[str] = set()
    serialised_wallets = []
    for e in entries:
        if e.name in seen:
            raise WalletNameConflict(
                f"duplicate wallet name in entries: {e.name!r}"
            )
        seen.add(e.name)
        _validate_priv_bytes(e.raw_key, f"wallet {e.name!r}")
        iv = secrets.token_bytes(AES_GCM_IV_BYTES)
        ct = aead.encrypt(iv, bytes(e.raw_key), e.name.encode("utf-8"))
        serialised_wallets.append({
            "name": e.name,
            "address": e.address,
            "iv_b64": _b64e(iv),
            "ciphertext_b64": _b64e(ct),
        })

    doc = {
        "version": CURRENT_VERSION,
        "kdf": "scrypt",
        "kdf_params": {
            "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt_b64": _b64e(salt),
        },
        "cipher": "aes-256-gcm",
        "wallets": serialised_wallets,
    }
    _atomic_write(p, doc, overwrite=True)


def get_salt(path: str | Path) -> bytes:
    """Return the keystore's salt without touching the passphrase.

    Used by ``add_wallet`` etc. to keep the salt stable across edits.
    """
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KeystoreCorrupt(f"read {p}: {exc}")
    try:
        return _b64d(doc["kdf_params"]["salt_b64"])
    except (KeyError, ValueError) as exc:
        raise KeystoreCorrupt(f"keystore {p} salt missing: {exc}")


def add_wallet(
    path: str | Path,
    passphrase: bytes,
    entry: KeystoreEntry,
) -> None:
    """Decrypt-then-re-encrypt to add a single wallet.

    We re-encrypt the whole file rather than appending in place because
    the format is JSON: rewriting is cheap, atomic, and keeps the
    invariant that every wallet uses a fresh IV.
    """
    p = Path(path)
    salt = get_salt(p)
    existing = load_keystore(p, passphrase)
    for e in existing:
        if e.name == entry.name:
            raise WalletNameConflict(
                f"wallet {entry.name!r} already in keystore"
            )
    save_keystore(p, passphrase, [*existing, entry], salt=salt)


def remove_wallet(
    path: str | Path, passphrase: bytes, name: str,
) -> KeystoreEntry:
    """Remove the named wallet, return the removed entry.

    Decrypting first lets us return the entry (for ``wallet export``-style
    flows) and proves the passphrase before we mutate the file.
    """
    p = Path(path)
    salt = get_salt(p)
    existing = load_keystore(p, passphrase)
    keep = [e for e in existing if e.name != name]
    if len(keep) == len(existing):
        raise KeystoreError(f"wallet {name!r} not in keystore")
    removed = next(e for e in existing if e.name == name)
    save_keystore(p, passphrase, keep, salt=salt)
    return removed


def rename_wallet(
    path: str | Path, passphrase: bytes, old: str, new: str,
) -> None:
    """Rename a wallet. AAD changes, so we must re-encrypt that entry."""
    p = Path(path)
    salt = get_salt(p)
    existing = load_keystore(p, passphrase)
    if any(e.name == new for e in existing):
        raise WalletNameConflict(f"wallet {new!r} already in keystore")
    found = False
    for e in existing:
        if e.name == old:
            e.name = new
            found = True
            break
    if not found:
        raise KeystoreError(f"wallet {old!r} not in keystore")
    save_keystore(p, passphrase, existing, salt=salt)


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, doc: dict, *, overwrite: bool) -> None:
    """Write JSON to ``path`` via a chmod-600 temp file + rename.

    On POSIX, ``rename`` is atomic across the same filesystem, so the
    file either contains the old keystore or the new one — never an
    interleaved write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Open with O_EXCL so a stale .tmp doesn't get reused.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC
    if tmp.exists():
        tmp.unlink()
    fd = os.open(str(tmp), flags, 0o600)
    try:
        os.write(fd, json.dumps(doc, indent=2).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    if path.exists() and not overwrite:
        tmp.unlink(missing_ok=True)
        raise KeystoreError(f"refusing to overwrite {path}")
    os.replace(str(tmp), str(path))
    # Belt-and-suspenders: ensure the final file is 0600 (replace
    # preserves source mode, but sources are 0600 above).
    try:
        os.chmod(str(path), 0o600)
    except OSError as exc:
        log.warning("chmod 600 %s failed: %s", path, exc)


# ---------------------------------------------------------------------------
# Passphrase resolution (env / file / stdin)
# ---------------------------------------------------------------------------


def resolve_passphrase(
    *,
    env_var_value: str | None = None,
    file_path: str | Path | None = None,
    allow_tty_prompt: bool = False,
) -> bytes:
    """Resolve the passphrase from one of three sources, in priority order:

      1. ``env_var_value`` — already-extracted env var (caller passes
         e.g. ``os.environ.get("KEY_PASSPHRASE")``). The caller is
         expected to ``del os.environ[var]`` after this.
      2. ``file_path`` — a chmod-600 file containing the passphrase.
         Trailing newline is stripped.
      3. ``allow_tty_prompt`` — if True and stdin is a TTY, prompt
         ``getpass.getpass``. Otherwise raise.

    Raises ``KeystoreError`` if no source provides a non-empty value.
    """
    if env_var_value:
        return env_var_value.encode("utf-8")

    if file_path:
        p = Path(file_path)
        try:
            st = p.stat()
        except FileNotFoundError:
            raise KeystoreError(f"passphrase file {p} not found")
        if st.st_mode & 0o077:
            raise KeystoreError(
                f"passphrase file {p} has unsafe mode "
                f"{stat.S_IMODE(st.st_mode):#o} — chmod 600 it"
            )
        try:
            data = p.read_bytes().rstrip(b"\r\n")
        except OSError as exc:
            raise KeystoreError(f"read {p}: {exc}")
        if not data:
            raise KeystoreError(f"passphrase file {p} is empty")
        return data

    if allow_tty_prompt and sys.stdin.isatty():
        import getpass
        pw = getpass.getpass("Keystore passphrase: ")
        if not pw:
            raise KeystoreError("empty passphrase")
        return pw.encode("utf-8")

    raise KeystoreError(
        "no passphrase source available: set KEY_PASSPHRASE, "
        "KEY_PASSPHRASE_FILE, or run interactively"
    )
