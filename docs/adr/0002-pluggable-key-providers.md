# ADR 0002: Pluggable secret backends via KeyProvider abstraction

## Status

Accepted. The four shipped backends (`env`, `file`, `keychain`,
`1password`) are part of the 1.x stable surface.

## Context

The TRON treasury private key has to come from somewhere. The
"somewhere" depends on the deployment shape, and the shapes we
actually run differ enough that there is no single right answer.

Concretely, the operator's deployments include:

- **Laptop / development.** 1Password is already where the operator
  stores secrets. Reusing it means one fewer secret store to maintain.
- **Containers.** An environment variable is the only ergonomic way
  to inject a secret into a container without baking it into the
  image. Compose, Kubernetes, ECS — they all do env-var secrets.
- **Bare-metal systemd.** A file at a known path with the right
  ownership and `chmod 600` is the canonical Unix pattern, and it's
  what `systemd-creds` and friends ultimately resolve to.
- **macOS local dev.** The operator has Keychain right there; using it
  avoids putting the key in a dotfile or in 1Password just for a
  development run.

A single hardcoded source would force all of these into one shape and
it's not clear which shape would be right for all four cases.

## Decision

**A `KeyProvider` Protocol with four backends.** The Protocol is
defined in `skr_crypto/server/key_providers.py`. Each backend
implements `load() -> bytes` returning the 32-byte private key. The
backend is selected via the `KEY_PROVIDER` env var, which takes one of
`env`, `file`, `keychain`, `1password`. Provider-specific configuration
lives in additional env vars (e.g. `OP_VAULT` and `OP_ITEM` for
1Password, `KEY_FILE_PATH` for file, etc.).

**Validation runs at boot, in `validate_config()`, before the listener
opens.** A misconfigured provider — wrong vault name, missing file,
file with wrong permissions, 1Password CLI not signed in — fails the
boot. The service does not start and there is no listener.

**The CLI's `keygen` and `doctor` know about all four backends.**
`skr-crypto keygen --write-to {env|file|keychain|1password}` writes
the generated key into the matching backend. `skr-crypto doctor`
checks all four for readiness and reports which is currently
selected.

## Consequences

**Each backend has its own setup gotchas.** The cost of supporting
four sources is that the operator has to know the gotchas for the one
they pick:

- `1password`: requires the `op` CLI installed and a session signed
  in. If the session expires, the next service restart fails.
- `env`: the value lives in the parent process's environment and is
  inheritable by any child process the service spawns. We don't spawn
  children deliberately, but a forking RPC library could leak it.
- `file`: requires `chmod 600` and the right ownership. The provider
  refuses to load if either is wrong.
- `keychain`: macOS-only. The keychain item must be in the login
  keychain, unlocked at the time the service starts. Useless on
  servers.

**The doctor command is the source of truth for "is this backend
configured right?"** Rather than scattering setup checks across
documentation, every backend's preconditions are encoded in
`doctor`'s checks. A FAIL there tells the operator exactly what to
fix.

**The keygen command knows how to write to each backend.** This was
not free — each backend's write path is different (1Password CLI
invocation, file create with `chmod 600` and refuse-to-overwrite,
keychain `security` invocation, env-var rewrite of `.env`). The
benefit is that operators don't have to know how each backend stores
secrets; the CLI handles it.

**Adding a new backend is a contained change.** Implement the
Protocol, register it in the dispatch dict, add a `doctor` check, add
a `keygen --write-to` branch. No code outside that module changes.

**The Protocol is intentionally narrow.** `load()` returns 32 bytes
and that's it. There is no `rotate()`, no `version()`, no async
variant. New methods would have to be added across all four backends,
and we have not yet found a use case that justifies that cost.

## Alternatives considered

**Hard-code 1Password.** Rejected for portability. 1Password requires
the `op` CLI and a network round-trip; that's fine on a laptop and
unacceptable in a container.

**Use HashiCorp Vault as the single backend.** Rejected as overkill
for a solo operator. Vault is the right answer at fleet scale and
brings real benefits — lease semantics, audit, dynamic secrets — but
deploying Vault for a single-key, single-host treasury is more
operational complexity than the alternative shapes combined.

**Environment variables only.** Rejected for two reasons. First, env
vars leak to child processes — even if we don't deliberately spawn
any, a future dependency might. Second, on systemd, env-var secrets
require either `EnvironmentFile=` (which is just a file with extra
steps) or `LoadCredential=` (which needs systemd-creds, which is a
fifth backend). Just letting the operator pick the one that fits is
simpler.

**A single config file with all secrets.** Rejected. That's the `file`
backend with extra steps; supporting "the file is JSON with multiple
keys" instead of "the file is the key" doesn't add anything except
parsing surface for bugs to live in.

## Related

- [`ARCHITECTURE.md` § Data flow with trust boundaries](../architecture.md)
- [`SECURITY.md` § Operator responsibilities](../security.md)
- `skr_crypto/server/key_providers.py`
