# 0006 — Encrypted-file keystore: AES-256-GCM + scrypt + per-entry IV with name-bound AAD

## Status

Accepted. 2026-04-30. Introduced in 1.4.0 alongside [ADR 0005](0005-multi-wallet-pool.md).

## Context

The four 1.3.x key providers each had a clean home except for one shape: **a Linux container without 1Password CLI access, that wants encrypted-at-rest keys without writing them to a host environment variable**. The available options were:

- `env` — keys live in the process environment. Visible in `/proc/<pid>/environ`, in any logging library that captured `os.environ` before we deleted the var, and in `docker inspect`. Encrypted at rest only if the orchestrator's secret store encrypts.
- `file` — keys live in plaintext on disk. Encrypted at rest only if the volume is encrypted.
- `1password` — needs interactive `op signin` + biometric. Doesn't work in a container.
- `keychain` — macOS only.

The pattern that emerged was: *operators wanted the 1Password "encrypted at rest, decrypted only in process memory" guarantee on a Linux container, without 1Password*. Plus, since multi-wallet was landing in the same release ([ADR 0005](0005-multi-wallet-pool.md)), they wanted the multi-wallet shape to be "obvious" rather than "set up N env vars per wallet."

So we needed a fifth provider that:

1. Encrypts the keys at rest with a passphrase.
2. Holds N wallets in one place.
3. Works inside a Docker container with no extra runtime dependencies.
4. Can be inspected (wallet count + advertised addresses) without the passphrase.
5. Has a format we can document end-to-end on one page.

## Decision

Define a versioned JSON keystore file with a single passphrase deriving an AES-256 key via scrypt. Each wallet is encrypted independently with a fresh per-entry IV; the wallet name is bound to the ciphertext via AEAD additional-data so swapping ciphertext blobs between entries fails authentication. Atomic writes via `O_EXCL` temp + `rename`.

Format (`version: 1`):

```json
{
  "version": 1,
  "kdf": "scrypt",
  "kdf_params": {"n": 32768, "r": 8, "p": 1, "salt_b64": "..."},
  "cipher": "aes-256-gcm",
  "wallets": [
    {"name": "main", "address": "T...", "iv_b64": "...", "ciphertext_b64": "..."}
  ]
}
```

Plaintext is the **32-byte raw private key**, not its hex representation. Ciphertext is `IV(12) + ct(32) + tag(16)` per entry. The AES key is derived once per file, reused across entries — adding the 100th wallet costs the same scrypt as the first.

Passphrase resolution chain at boot:

1. `KEY_PASSPHRASE` env var (consumed and removed from `os.environ` after read).
2. `KEY_PASSPHRASE_FILE` chmod-600 file.
3. Interactive `getpass()` if stdin is a TTY.

The CLI's `wallet` group manipulates this file directly (`add`, `remove`, `rename`, `encrypt`). For other providers the same commands print env-var / file-path instructions for the operator to apply manually.

## Consequences

**Container-friendly.** Docker / k8s / Fly.io / etc. can mount the keystore as a volume and inject the passphrase via a secret. The shipped `docker-compose.yml` demonstrates the pattern.

**Multi-wallet by construction.** No `WALLETS=name1,name2` env var to keep in sync; the file *is* the list. `skr-crypto wallet generate cold` adds an entry; restart picks it up.

**Auditable.** Anyone who suspects the format can read the file, decode the base64 fields, and verify the cryptographic shape with 30 lines of Python. We deliberately did not pick a binary container format that requires a tool to read.

**One passphrase gates everything.** A compromised passphrase compromises every wallet in the file. We accept this trade-off — partitioning by separate keystore files is a deployment-level decision, not a format-level one. Operators who want per-wallet KDF can run multiple service instances.

**Lose the passphrase, lose the keys.** There's no recovery. The file format does not embed any escrow or sharing mechanism. Operators must back up the passphrase the way they back up any other one-of-a-kind secret.

**Atomic writes mean a crash mid-mutation cannot corrupt the file.** Worst case is a stale `.tmp` sibling, which the next operation cleans up.

The cost of `scrypt(n=32768, r=8, p=1)` is ~30 ms / ~32 MB on modern hardware — adds to boot time, irrelevant for steady-state operation.

## Alternatives considered

- **age (https://age-encryption.org/).** Modern age-encryption format, well-reviewed. Rejected because `pyrage` is not on every wheel platform we ship to, and the Python age implementations are not as mature as `cryptography`. We already depend on `cryptography` for nothing more than this; using a built-in primitive set is simpler.
- **GPG.** Decade-plus reputation, ubiquitous. Rejected because GPG agent + TTY interaction is awful in containers, the format is heavy, and the keyring is a moving part we don't need.
- **gocryptfs / EncFS / overlay encryption.** Overkill for one file; pulls in a kernel module + filesystem mount; harder to back up, harder to ship.
- **Argon2id instead of scrypt.** Marginally better KDF properties on dedicated-hardware attackers. Rejected because adopting it adds `argon2-cffi` to the wheel set, which is a lot of cost for a passphrase that's typed once per process. scrypt is in `cryptography` already and is well above the bar for this use case.
- **PBKDF2.** Would have been the easiest. Rejected because it's GPU-friendly — exactly the wrong property for a passphrase-derived key.
- **One single key for the whole file (no per-entry IV).** Simpler format. Rejected because if two operators happened to provision wallets with the same key (yes, it would be silly, but it has happened), they'd produce identical ciphertext blobs and be visibly identical in the file — a low-effort information leak.
- **No AAD binding.** Slightly simpler. Rejected because without it, an attacker with write access to the file (but no passphrase) could swap the `name` field on a ciphertext entry and trick the operator into thinking they're sending from "cold" when they're actually signing with "hot". Binding the name to the AEAD tag closes this.
- **ChaCha20-Poly1305 instead of AES-256-GCM.** Equivalent security; AES-NI is universal on the platforms we'd run, so AES-GCM is marginally faster. The `cryptography` API for `AESGCM` is also slightly simpler than `ChaCha20Poly1305` — fewer keyword arguments to remember. Tie broken on dev ergonomics.
- **Hex-encoded plaintext.** The encrypted blob would decrypt to 64 ASCII hex chars instead of 32 raw bytes. Rejected because round-tripping back to bytes adds an intermediate copy on the heap (worse for memory hygiene) and an extra failure mode (bad hex inside the decrypt path).
- **Public-key construction (one keypair per wallet, public material in clear).** Would let you encrypt new wallets without the passphrase. Rejected because it complicates the format substantially for a use case (delegated wallet provisioning) we don't have.

## Related

- [Encrypted keystore format](../keystore-format.md) — operator-facing spec.
- [Key providers](../key-providers.md) — comparison with the four other backends.
- [ADR 0005](0005-multi-wallet-pool.md) — the multi-wallet shape this provider was built to fit.
- [ADR 0002](0002-pluggable-key-providers.md) — the provider abstraction this plugs into.
