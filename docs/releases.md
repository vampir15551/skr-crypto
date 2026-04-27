# Release Checklist

Versioning follows [SemVer](https://semver.org/). Pre-1.0 we reserve
the right to break things in a minor release if the operational lesson
is severe enough — but every break gets a `**Breaking:**` line in
`CHANGELOG.md` and a migration note.

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

# Tag (annotated, signed if you have a GPG key)
git tag -s v0.2.0 -m "v0.2.0 — see CHANGELOG.md"

# Push code + tag in one go
git push origin main v0.2.0
```

The `release.yml` workflow takes it from there:
1. Builds the wheel + sdist.
2. Verifies the tag matches `__version__`.
3. Publishes to PyPI via OIDC trusted publishing.
4. Opens a GitHub Release with the matching CHANGELOG section as the body.

If trusted publishing isn't set up yet, the first release fails at
step 3 with "OIDC token rejected" — go to PyPI → project →
Publishing → "Add a new publisher" with:
- Owner: `vampir15551`
- Repo: `skr-crypto`
- Workflow filename: `release.yml`
- Environment: `pypi`

## Verify the release

- [ ] `pip install -U skr-crypto` (in a clean venv) installs the new
      version.
- [ ] `skr-crypto --version` prints the new number.
- [ ] PyPI page shows the new release: <https://pypi.org/project/skr-crypto/>
- [ ] GH Release page exists with the changelog body and the wheel
      attached.
- [ ] If anyone's depending on the docs site — refresh and confirm
      the new pages are live.

## Rollback

PyPI doesn't allow re-uploading a yanked version, but you can yank
broken versions so `pip install` skips them:

```bash
# On pypi.org, click "Yank release" — users keeping the version
# pinned still get it; default `pip install` skips it.
```

For users on the broken version, push `v0.2.1` with the fix —
yanking + a new patch is cheaper than chasing a deleted tag.

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
