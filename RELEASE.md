# Release Checklist

Versioning follows [SemVer](https://semver.org/). Pre-1.0 we reserve
the right to break things in a minor release if the operational lesson
is severe enough — but every break gets a `**Breaking:**` line in
`CHANGELOG.md` and a migration note.

Distribution is **private** — wheels are attached to GitHub Releases
on tag push. There is **no PyPI upload**. Consumers install with
`pip install <wheel-url>` (the URL from the release page) or by
cloning the repo and `pip install -e .`.

## Pre-release

- [ ] All tests green: `pytest -v`
- [ ] Lint clean: `ruff check skr_crypto tests`
- [ ] Docs build clean: `mkdocs build --strict`
- [ ] `CHANGELOG.md` `[Unreleased]` section captures everything since
      the last tag. Move it under a new `[X.Y.Z] — YYYY-MM-DD`
      heading; open a fresh `[Unreleased]` block above it.
- [ ] `skr_crypto/version.py`'s `__version__` matches the new tag
      (release CI fails the build if they drift).
- [ ] Smoke-test locally with the new build:
  ```
  pip install -e ".[dev]"
  skr-crypto --version
  skr-crypto help
  skr-crypto doctor
  ```

## Cut the tag

```bash
# Look at what's about to ship
git diff $(git describe --tags --abbrev=0)..HEAD

# Tag (annotated + signed — see CONTRIBUTING for one-time signing setup)
git tag -s v0.2.0 -m "v0.2.0 — see CHANGELOG.md"

# Push code + tag in one go
git push origin main v0.2.0
```

The `release.yml` workflow takes it from there:

1. Builds the wheel + sdist.
2. Verifies the tag matches `__version__` (fails the run otherwise).
3. Creates a GitHub Release with the matching CHANGELOG section as
   the body and the wheel + sdist attached.

## Verify the release

- [ ] GitHub Release page exists with the changelog body and the
      wheel attached:
      <https://github.com/vampir15551/skr-crypto/releases>
- [ ] In a clean venv on the target host:
  ```
  python3.12 -m venv /tmp/skr-fresh
  /tmp/skr-fresh/bin/pip install \
      https://github.com/vampir15551/skr-crypto/releases/download/v0.2.0/skr_crypto-0.2.0-py3-none-any.whl
  /tmp/skr-fresh/bin/skr-crypto --version  # → 0.2.0
  ```
- [ ] If anyone else uses the tool, ping them with the release URL.

## Rollback

GitHub Releases can be deleted (`gh release delete v0.2.0 --yes`),
which removes the wheel and the release page. The git tag remains
unless you also delete it (`git tag -d v0.2.0 && git push --delete
origin v0.2.0`). Generally prefer **shipping a v0.2.1 fix** to
deleting a tag — fewer footguns, cleaner history.

## Versioning policy details

- **Major (X.0.0)**: anything that changes a CLI command's exit code,
  removes a flag, breaks the on-disk install layout, or requires the
  operator to migrate state.
- **Minor (0.X.0)**: new commands, new flags with safe defaults, new
  endpoints supported, behavioural improvements that don't change
  exit codes for existing flows.
- **Patch (0.0.X)**: bug fixes, doc-only changes, dep bumps without
  observable behaviour change.

Pre-1.0 only: minor versions may include carefully-scoped breaks if
keeping compatibility would be more confusing than the break itself
(e.g. renaming a flag that nobody uses correctly). Document with a
`**Breaking:**` line in the CHANGELOG.
