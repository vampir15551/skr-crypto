# Releasing

> Engineer-facing runbook for cutting a `skr-crypto` release. This file
> supersedes the older `RELEASE.md`. If you're an operator looking for
> install instructions, see [`README.md`](README.md) — this document is
> for maintainers.

Distribution is **private**. Wheels are attached to GitHub Releases on
tag push; there is no PyPI upload and there isn't going to be one.
Consumers install with `pip install <wheel-url>` from the release page.

---

## Versioning policy

We follow [Semantic Versioning 2.0.0](https://semver.org/) post-1.0.

- **Major (X.0.0)**: any change that breaks an existing operator's
  workflow without action — removed CLI command, removed flag, changed
  exit code semantics, on-disk layout migration, env-var rename, API
  endpoint removal. Major releases require a deprecation cycle of at
  least one minor with a `**Deprecated:**` line in the CHANGELOG.
- **Minor (X.Y.0)**: new commands, new flags with safe defaults, new
  API endpoints, new KeyProvider backends, behavioural improvements
  that don't change exit codes or response shapes for existing flows.
- **Patch (X.Y.Z)**: bug fixes, docs-only changes, dep bumps without
  observable behaviour change, security patches.

**The pre-1.0 carve-out is closed.** Pre-1.0 reserved the right to
break things in a minor; we are now at 1.0.0 and that escape hatch is
gone. Breaking changes go in major versions.

---

## Pre-release checklist

Run these in order. A failure on any line is a release blocker — fix it
or roll back the proposed bump.

- [ ] **Tests green:** `pytest -v`
- [ ] **Lint clean:** `ruff check skr_crypto tests`
- [ ] **Docs build clean (strict):** `mkdocs build --strict`
- [ ] **Security scans clean or annotated:**
  - `bandit -r skr_crypto/server` (no high-severity findings)
  - `pip-audit` (no unfixed advisories on direct deps; transitive
    advisories with no upstream fix get a CHANGELOG annotation)
- [ ] **CHANGELOG `[Unreleased]` non-empty and accurate.** Walk
  `git log $(git describe --tags --abbrev=0)..HEAD` and verify each
  user-visible change has an entry.
- [ ] **Version bump committed.** `skr_crypto/version.py`'s
  `__version__` matches the proposed tag exactly. The release CI job
  asserts this and fails the build if they drift.
- [ ] **Smoke install from a clean venv:**
  ```bash
  python3.12 -m venv /tmp/skr-smoke && \
      /tmp/skr-smoke/bin/pip install -e ".[dev,server]" && \
      /tmp/skr-smoke/bin/skr-crypto --version && \
      /tmp/skr-smoke/bin/skr-crypto doctor
  ```
- [ ] **CHANGELOG `[Unreleased]` moved** under a new `[X.Y.Z] — YYYY-MM-DD`
  heading with a fresh empty `[Unreleased]` block above it.

---

## Cut the tag

Once the checklist is green, tag and push. The exact commands:

```bash
# 1. Confirm what's about to ship.
git diff $(git describe --tags --abbrev=0)..HEAD

# 2. Annotated, signed tag (SSH ed25519, see CONTRIBUTING for one-time setup).
git tag -s vX.Y.Z -m "vX.Y.Z — see CHANGELOG.md"

# 3. Push code + tag in one go.
git push origin main vX.Y.Z
```

Notes:

- The tag MUST be annotated (`-a` is implied by `-s`). Lightweight tags
  are rejected by the release workflow.
- The tag MUST be signed (`-s`). `git config gpg.format ssh` and
  `user.signingkey` are configured per the maintainer setup; the
  workflow does not check the signature itself but GitHub displays
  `Verified` for signed tags and we want that visible on the release
  page.
- The tag name MUST match `^v\d+\.\d+\.\d+$`. Pre-releases (`-rc1`,
  `-beta`) are not used; we ship from `main` and skip release
  candidates.

---

## What CI does on tag push

The `release.yml` workflow triggers on `push` to a `v*.*.*` tag. It:

1. **Builds** wheel + sdist via `python -m build`.
2. **Verifies** the tag exactly matches `skr_crypto.version.__version__`.
   Mismatch fails the run; nothing is published.
3. **Computes `SHA256SUMS.txt`** over the two artefacts.
4. **Creates a GitHub Release** named `vX.Y.Z` with:
   - The matching `[X.Y.Z]` section of `CHANGELOG.md` extracted as the
     release body.
   - The wheel, sdist, and `SHA256SUMS.txt` attached.
5. **Marks the release as Latest** (the default for non-prerelease tags).

Workflow runtime is roughly 90 seconds; if it takes longer than three
minutes, something is wrong — check the workflow run page.

---

## Post-release verification

The release exists when the workflow says it does, but verify it
end-to-end before declaring success:

```bash
# 1. The release page exists and has three artefacts.
gh release view vX.Y.Z --repo vampir15551/skr-crypto

# 2. The wheel installs cleanly into a fresh venv.
python3.12 -m venv /tmp/skr-fresh
/tmp/skr-fresh/bin/pip install \
    https://github.com/vampir15551/skr-crypto/releases/download/vX.Y.Z/skr_crypto-X.Y.Z-py3-none-any.whl

# 3. Version matches.
/tmp/skr-fresh/bin/skr-crypto --version  # → X.Y.Z

# 4. Doctor passes.
/tmp/skr-fresh/bin/skr-crypto doctor

# 5. Checksums verify.
( cd /tmp && \
  curl -LO https://github.com/vampir15551/skr-crypto/releases/download/vX.Y.Z/SHA256SUMS.txt && \
  curl -LO https://github.com/vampir15551/skr-crypto/releases/download/vX.Y.Z/skr_crypto-X.Y.Z-py3-none-any.whl && \
  sha256sum -c SHA256SUMS.txt )
```

Any of these failing means the release is broken — see Rollback.

---

## Rollback

`gh release delete vX.Y.Z --yes` removes the wheel and the release page
but leaves the tag in place. Tag deletion (`git tag -d vX.Y.Z &&
git push --delete origin vX.Y.Z`) is technically possible but is the
wrong move 95% of the time.

**Prefer shipping a `vX.Y.(Z+1)` fix** over deleting a tag. Reasons:

- A deleted tag may already be cached in someone's local clone or in a
  Docker image layer; deleting it remotely doesn't make those copies
  go away.
- The CHANGELOG history reads cleaner with a forward-progress patch
  release than with a "this version doesn't exist anymore" gap.
- Anyone who installed `vX.Y.Z` between the bad release and the
  rollback now has an installed package that no longer matches a
  published artefact — confusing during incident triage.

The only legitimate reason to delete a tag is "the wheel contains a
secret that was accidentally committed". In that case, delete fast and
follow the credential rotation procedure in
[`SECURITY.md`](SECURITY.md#what-you-must-rotate-after-a-suspected-leak).

---

## Hotfix branches

Most fixes ship from `main` as the next patch release. The exception is
a security patch that needs to land on an older supported minor — for
example, `1.4.7` is current, `1.3.x` is still supported, and a CVE
affects both.

The procedure:

```bash
# 1. Branch from the tag that needs the fix.
git checkout -b hotfix/1.3.x v1.3.6

# 2. Cherry-pick the fix from main.
git cherry-pick <sha-on-main>

# 3. Bump version and CHANGELOG locally.
# version.py → 1.3.7
# CHANGELOG: add a [1.3.7] section under [Unreleased].

# 4. Tag, push, let CI cut the release.
git tag -s v1.3.7 -m "v1.3.7 — security patch"
git push origin hotfix/1.3.x v1.3.7
```

The hotfix branch is short-lived; delete it once the release is out
(`git push --delete origin hotfix/1.3.x` after merging any
non-cherry-picked changes back to `main`, which is rare).

Do not run hotfixes on `main`. The point of the branch is that `main`
may have already moved on with changes that aren't safe to ship in a
patch release.

---

## Communicating a release

The GitHub Release page **is** the announcement. We don't run a mailing
list, we don't post to social media, we don't update any third-party
registries.

The CHANGELOG section that goes into the release body should be:

- **Operator-focused.** Phrase changes in terms of what the operator
  will see: "doctor now warns when…", not "refactored doctor.py".
- **Terse.** One line per change. Group by Added / Changed / Fixed /
  Removed / Security. No marketing prose.
- **Action-oriented for breaking changes.** A breaking change must
  include the migration step inline; "see UPGRADE.md" is a fail-state
  for the changelog.

If a release contains a security fix, add a `### Security` section at
the top with a brief description and a CVE/advisory link if applicable.
Operators who skim release notes look for that heading first.

That's it. The release goes out, operators see it on the releases page,
they run `skr-crypto update`. No further communication is required.
