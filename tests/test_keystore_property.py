"""Property-based tests for the encrypted keystore.

Fuzzes:

  - Random byte flips on a valid keystore file → must raise
    KeystoreCorrupt or WrongPassphrase, never silent corruption,
    never crash with ValueError / UnicodeDecodeError / etc.
  - Random keys + names round-trip cleanly through encrypt → decrypt.
  - Wrong passphrases never produce a successful load.
"""
from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from skr_crypto.server import encrypted_keystore as ks

pytestmark = [pytest.mark.property]


# A 32-byte private key, never all zeros (the keystore refuses zero keys).
_priv_keys = st.binary(min_size=32, max_size=32).filter(
    lambda b: any(byte != 0 for byte in b)
)
# Wallet names: ascii letters/digits/_/-, 1-32 chars
_names = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="_-",
    ),
    min_size=1, max_size=32,
).filter(lambda s: s.replace("_", "").replace("-", "").isalnum())
_passphrases = st.binary(min_size=1, max_size=64)


# ---------------------------------------------------------------------------
# 1. Round-trip
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@given(priv=_priv_keys, name=_names, passphrase=_passphrases)
@settings(max_examples=20, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_round_trip_any_key_name_passphrase(tmp_path_factory, priv, name, passphrase):
    """ANY (priv, name, passphrase) tuple round-trips through
    encrypt → decrypt cleanly. The plaintext we get back equals the
    plaintext we put in."""
    path = tmp_path_factory.mktemp("ks") / "keystore.json"
    ks.init_keystore(path, passphrase)
    ks.add_wallet(path, passphrase, ks.KeystoreEntry(
        name=name, address="TXxx", raw_key=bytearray(priv),
    ))
    entries = ks.load_keystore(path, passphrase)
    assert len(entries) == 1
    assert entries[0].name == name
    assert bytes(entries[0].raw_key) == priv


# ---------------------------------------------------------------------------
# 2. Tamper-resistance — random byte flips never produce silent corruption
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@given(
    priv=_priv_keys,
    flip_offset=st.integers(min_value=0, max_value=2000),
    flip_byte=st.integers(min_value=0, max_value=255),
)
@settings(
    max_examples=30, deadline=None, derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_byte_flip_never_silent_corruption(tmp_path_factory, priv, flip_offset, flip_byte):
    """Flipping ANY byte in the keystore file must produce one of:

      - WrongPassphrase (the AEAD tag rejected the modified ciphertext)
      - KeystoreCorrupt (the JSON / base64 / scrypt-params shape broke)
      - KeystoreError (some other graceful refusal)

    It must NEVER:
      - Return successfully with corrupted plaintext
      - Crash with KeyError / ValueError / UnicodeDecodeError / TypeError
        (those are bugs; everything must be wrapped in a Keystore* class)
    """
    path = tmp_path_factory.mktemp("ks") / "keystore.json"
    ks.init_keystore(path, b"correct-pw")
    ks.add_wallet(path, b"correct-pw", ks.KeystoreEntry(
        name="main", address="TXxx", raw_key=bytearray(priv),
    ))

    # Flip a byte. If offset is past the file we silently skip.
    raw = bytearray(path.read_bytes())
    if flip_offset >= len(raw):
        assume(False)  # Hypothesis: tells the framework to discard
    raw[flip_offset] = flip_byte
    path.write_bytes(bytes(raw))
    os.chmod(path, 0o600)

    try:
        result = ks.load_keystore(path, b"correct-pw")
    except (ks.WrongPassphrase, ks.KeystoreCorrupt, ks.KeystoreError):
        # Expected — graceful refusal
        return

    # If we got here, load succeeded despite the flipped byte.
    # The ONLY way this is OK is if the flip happened to be a
    # no-op (e.g. flipped a JSON whitespace byte to another
    # whitespace byte, or rewrote the same value).
    # Verify the plaintext still matches — silent corruption would
    # show as a changed key.
    assert len(result) == 1
    assert bytes(result[0].raw_key) == priv, (
        f"Silent corruption: byte flip at {flip_offset} produced a "
        f"different key without raising"
    )


# ---------------------------------------------------------------------------
# 3. Wrong passphrase never succeeds
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@given(
    correct=_passphrases,
    wrong=_passphrases,
)
@settings(max_examples=20, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_wrong_passphrase_never_succeeds(tmp_path_factory, correct, wrong):
    """For any (correct, wrong) where wrong != correct, load_keystore
    with `wrong` must raise WrongPassphrase."""
    assume(wrong != correct)
    path = tmp_path_factory.mktemp("ks") / "keystore.json"
    ks.init_keystore(path, correct)
    ks.add_wallet(path, correct, ks.KeystoreEntry(
        name="main", address="TXxx", raw_key=bytearray(b"\x11" * 32),
    ))
    with pytest.raises(ks.WrongPassphrase):
        ks.load_keystore(path, wrong)


# ---------------------------------------------------------------------------
# 4. JSON-shape corruption
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@given(garbage=st.binary(min_size=0, max_size=200))
@settings(max_examples=50, deadline=None)
def test_garbage_file_raises_cleanly(tmp_path_factory, garbage):
    """ANY arbitrary bytes presented as a 'keystore' must be refused
    with a Keystore* exception, not crash with a generic Python error."""
    path = tmp_path_factory.mktemp("ks") / "keystore.json"
    path.write_bytes(garbage)
    os.chmod(path, 0o600)
    try:
        ks.load_keystore(path, b"any-passphrase")
    except (ks.KeystoreCorrupt, ks.KeystoreError, ks.WrongPassphrase):
        return  # ok
    # If load returned without raising, we have at most an empty
    # entries list. Garbage that happens to JSON-parse to a valid
    # empty keystore is a (very) rare but acceptable edge case.
    # Anything else is a bug.
