# Security Policy

> Threat model, supported versions, vulnerability disclosure, and operator
> responsibilities for `skr-crypto`. This document is authoritative; runbook
> details live in [`OPERATIONS.md`](OPERATIONS.md) and engineering principles
> live in [`GUIDELINES.md`](GUIDELINES.md).

---

## Reporting a vulnerability

**Do not open a public issue** for anything that touches auth, the private
key, the audit trail, the idempotency state machine, or the read-only
boundary between the CLI and the service.

Use one of these two channels, in order of preference:

1. **GitHub private security advisory.** Open one at
   <https://github.com/vampir15551/skr-crypto/security/advisories/new>.
   This is the preferred channel; it gives us a private workspace, CVE
   coordination if applicable, and a clean paper trail.
2. **Email the maintainer.** If you can't or won't use GitHub, email the
   address listed on the `vampir15551` GitHub profile. Encrypted email is
   not required — see [Cryptographic posture](#cryptographic-posture) for
   why we don't run a PGP key.

What to include:

- A minimal reproduction. Code, curl invocation, or sequence of CLI calls.
- The version you were running (`skr-crypto --version`).
- The platform (`uname -a`, distro, Python version).
- Your assessment of severity and impact.

What we commit to:

- **Acknowledge within 72 hours** of receipt. If you don't hear back in
  that window, assume the message was lost and re-send via the other
  channel.
- **Patch within 14 days** for anything graded high or critical. Lower
  severities ship in the next routine release.
- **Coordinated disclosure.** We'll agree on a disclosure date with you;
  the default is "after a patched release is out and operators have had
  one week to update".
- **Credit in the changelog and the GitHub Release notes**, unless you
  ask us not to.

---

## Supported versions

Security patches land on supported branches only. Older releases get
nothing — operators are expected to update.

| Version | Status              | Security patches      |
|---------|---------------------|-----------------------|
| 1.x     | Current             | Yes                   |
| 0.x     | Pre-1.0 — unsupported | No                  |
| < 0.x   | N/A                 | N/A                   |

Pre-1.0 explicitly received no security backports — the carve-out is
documented in `RELEASING.md` and `GUIDELINES.md`. Anyone still running a
0.x build should update before reporting.

When 2.x exists, 1.x will receive best-effort patches for security issues
on its latest minor for one minor's worth of overlap, then drop. We'll
announce the cutoff in the CHANGELOG before it lands.

---

## Threat model

### Assets

In descending order of value to an attacker.

1. **TRON treasury private key** (asset #1, highest). Compromise = drain.
   This is the only asset whose loss is irrecoverable — there is no
   undo, no insurance, no forensic recovery. Every other defense in this
   document exists to protect it.
2. **Audit log + idempotency database** (asset #2, integrity over
   confidentiality). The audit log is the system of record for what the
   service did; tampering with it breaks the operator's ability to know
   the truth. The idempotency DB prevents double-spend on retry. Reading
   either is unpleasant; corrupting either is dangerous.
3. **`AUTH_TOKEN`** (asset #3). Whoever has this can call
   `POST /api/v1/send` and request payouts, subject to the service's
   internal balance and energy limits. It is not the key, but it is the
   gate to the key's signing power.
4. **Recipient addresses** (asset #4, integrity). If an attacker can
   substitute a recipient address inside the request body before it
   reaches the service, the service will sign and broadcast the wrong
   payout. Defense lives in the request integrity layer (HTTPS terminated
   at the reverse proxy + `AUTH_TOKEN` over that channel).

### Adversaries we defend against

1. **Network attacker** on the path between client and service. Mitigated
   by terminating TLS at the operator's reverse proxy (the service does
   not terminate TLS itself), constant-time `AUTH_TOKEN` comparison, and
   per-IP rate limiting in the service.
2. **Local non-root user** sharing the host. Mitigated by `chmod 600` on
   `.env`, by the service running under a dedicated non-root user, and
   by `KEY_PROVIDER=keychain` / `1password` keeping the key out of any
   file the local user can read.
3. **Compromised dependency** — a malicious bump of `tronpy`, `click`,
   `requests`, or any transitive. Mitigated by version pins in
   `requirements.txt`, Dependabot for visibility, signed commits and
   tags so an attacker can't masquerade as the maintainer, and
   `--require-hashes` when installing from the lockfile.
4. **Operator mistake** — wrong recipient address, double-running a send,
   misreading a balance. Mitigated by the idempotency contract (same key,
   same outcome), the durable audit log (operator can verify what
   happened), the read-only CLI on the money path, and explicit
   confirmation prompts on destructive CLI commands.

### Adversaries we explicitly do NOT defend against

These are out of scope. A report claiming a vulnerability in one of these
areas will be closed as `wontfix` with a pointer here.

- **Root on the host.** Owns everything regardless. The threat model
  starts at "operator has not been root-compromised".
- **Memory forensics on swapped pages.** The private key lives in process
  memory while the service runs. We do not `mlock`, we do not zero pages,
  we do not disable swap. An attacker with `/proc/<pid>/mem` access wins.
- **Side-channel timing attacks** on the host CPU. We use constant-time
  comparison for `AUTH_TOKEN`, but we do not rebalance signing operations
  or hide branch behaviour against a co-resident process.
- **Hostile RPC.** We trust TronGrid to report chain state honestly. A
  malicious RPC could lie about balances or transaction inclusion. The
  defense against this is operator vigilance + the audit log: every
  broadcast records the txid, and the operator can independently verify
  on-chain.

### Properties / invariants

These are the security-relevant guarantees the system makes. A bug that
violates one of these is, by definition, a security bug.

- **Idempotency is a hard contract.** No double-spend across replay.
  Same `Idempotency-Key`, same outcome — exactly. An `UNRESOLVED` slot
  refuses retry, ever, automatically.
- **Audit is immutable on the success path.** Every `SEND_SUCCESS` row
  is `fsync`'d to `audit.log` before `/api/v1/send` returns 200. If the
  audit write fails, the request fails; there is no silent degradation.
- **Private key never reaches disk** under the `env` / `keychain` /
  `1password` providers. Under the `file` provider it sits at rest at a
  path the operator chose, with `chmod 600` and ownership enforced at
  load time.
- **Private key never exits process memory.** Not in audit rows, not in
  HTTP responses, not in stdout, not in logs, not in the idempotency DB.
- **CLI is read-only on the money path.** There is no `skr-crypto send`,
  there will never be one. The CLI cannot sign or broadcast.

---

## Boundaries

**HTTP API authentication.** A single `AUTH_TOKEN` is checked against the
inbound `X-API-Key` header using constant-time comparison
(`hmac.compare_digest`). There is no per-caller identity; rotation is
performed by editing `.env` and restarting the service. The token is a
shared secret between the operator and any script that calls the API.

**TLS.** The service does **not** terminate TLS. Operators are expected
to put a reverse proxy (nginx, Caddy, Traefik) in front of it for
production deployments. The default bind is `127.0.0.1:8000`, which is
loopback-only and acceptable without TLS for same-host clients.

**Rate limiting.** Per-IP, in-memory, sliding window. Defaults to 30
requests per 60 seconds per IP. Configured via `RATE_LIMIT_MAX` and
`RATE_LIMIT_WINDOW`. This is a load shield, not a security boundary —
behind a reverse proxy, ensure `TRUSTED_PROXIES` is set so the per-IP
counter sees the real client.

**Trust boundary inside the process.** The KeyProvider runs once at boot.
After that, the private key is bound to the `TronClient` singleton and
is reachable only from the broadcast call site. New code that touches
`tron.priv_key` should be reviewed with that boundary in mind.

---

## Cryptographic posture

**Commit and tag signing.** All commits and tags are signed using the
maintainer's ed25519 SSH key. GitHub displays `Verified` on signed
commits when the same key is registered as a Signing Key on the GitHub
profile.

We use SSH signing rather than GPG/PGP for two reasons:

1. The operator already has an ed25519 SSH key for git push; reusing it
   means one fewer key to manage and rotate.
2. PGP key management is a known operational hazard. Maintaining a web
   of trust, distributing public keys, and rotating expired keys are all
   failure modes we don't need.

**Release artefact integrity.** Every GitHub Release has three artefacts
attached: the wheel, the sdist, and `SHA256SUMS.txt`. The `SHA256SUMS.txt`
is computed by the CI workflow and committed to the release at the same
time as the wheel. To verify a download:

```bash
curl -LO https://github.com/vampir15551/skr-crypto/releases/download/vX.Y.Z/skr_crypto-X.Y.Z-py3-none-any.whl
curl -LO https://github.com/vampir15551/skr-crypto/releases/download/vX.Y.Z/SHA256SUMS.txt
sha256sum -c SHA256SUMS.txt
```

A failure here is a security event — report it via the channels above.

We do not sign the wheel or sdist directly. The signed git tag covers the
source tree; the SHA256SUMS file covers the build artefacts; the GitHub
Release page binds them together. This is sufficient for our threat model
and avoids the operational cost of a separate signing key.

---

## Operator responsibilities

The service is secure only if the operator does these things. The CLI's
`doctor` command checks most of them and exits 1 on any drift.

- **`chmod 600` on `.env`.** `skr-crypto install` writes it that way.
  `skr-crypto doctor` flags any drift. Don't loosen the permissions, even
  temporarily.
- **`AUTH_TOKEN` secrecy.** Treat `.env` like a credential file. Do not
  commit it. Do not paste it in chat or screenshots. Do not include it in
  bug reports — `skr-crypto config show` masks it by default.
- **Key rotation procedure.** Rotation is a treasury operation, not a
  configuration change. The runbook is in [`OPERATIONS.md`](OPERATIONS.md);
  it covers draining the old address, generating a new key, updating the
  KeyProvider source, and verifying.
- **Update discipline.** Run `skr-crypto update` regularly. A security
  patch in `tronpy` or `requests` only protects you once it's installed.
  We do not auto-update — silent breakage is worse than known stale.
- **Never run as root.** The service refuses to start as root and the
  CLI's `doctor` flags it as a FAIL. Run under a dedicated non-root user
  with the minimum file permissions needed to read `.env` and write
  `data/`.
- **Reverse proxy for any non-loopback bind.** If you set `SERVER_HOST=0.0.0.0`
  or expose the service beyond the loopback, terminate TLS upstream and
  set `TRUSTED_PROXIES` correctly so the rate limiter sees the real IP.

---

## What you must rotate after a suspected leak

Suspected leak means: `.env` was committed to a public repo by mistake,
a backup tarball ended up in cloud storage with the wrong ACL, a
laptop with the key was lost, a process dump was uploaded to a paste
site. If you're not sure whether something counts, treat it as a leak.

Rotate in this order. Each step assumes the previous one is complete.

1. **Private key.** Generate a fresh key (`skr-crypto keygen`), fund the
   new address, and **drain the compromised address to the new one**. Do
   this first — every minute the compromised key is on a funded address
   is a minute an attacker can drain it. Confirm the drain on-chain
   before moving on.
2. **`AUTH_TOKEN`.** Edit `.env` to a fresh high-entropy value (the
   `keygen` command will accept `--token-only` to generate one for you,
   or use `python -c 'import secrets; print(secrets.token_urlsafe(32))'`).
   Restart the service. Update every script and operator that calls the
   API.
3. **`TRONGRID_API_KEY`** if you suspect the leak included it. Rotate it
   in the TronGrid dashboard, update `.env`, restart.

After rotation, file an internal post-mortem: what leaked, how it leaked,
what changed to prevent recurrence. The audit log of the old address is
preserved in `data/audit.log` — keep it for compliance even after the
address is drained.
