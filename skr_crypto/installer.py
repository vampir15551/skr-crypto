"""git clone + venv + pip install — the mechanical part of `install`/`update`.

Kept separate from the click commands so it's testable end-to-end via
``tmp_path`` and a local fixture repo, without any click context.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from skr_crypto.exceptions import GitOperationError, VenvError


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command. Raises a typed exception on failure."""
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=capture,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        # Caller decides which exception to raise — we just attach details.
        cmd_str = " ".join(cmd)
        raise RuntimeError(
            f"command failed (exit {result.returncode}): {cmd_str}\n"
            f"stderr: {(result.stderr or '').strip()}"
        )
    return result


def git_clone(url: str, ref: str, dest: Path) -> None:
    """Clone ``url`` into ``dest`` and check out ``ref``.

    ``ref`` can be a branch name, a tag, or a full commit sha. We use a
    shallow clone (``--depth 1``) for speed, then unshallow only if the
    requested ref isn't reachable from the default branch's tip — which
    is rare in practice (most users run ``install`` against ``main``
    or a recent tag).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(["git", "clone", "--depth", "1", "--branch", ref, url, str(dest)])
    except RuntimeError as exc:
        # Fallback: full clone + checkout. Costs more bandwidth but
        # works for old tags / commit shas.
        try:
            _run(["git", "clone", url, str(dest)])
            _run(["git", "checkout", ref], cwd=dest)
        except RuntimeError as exc2:
            raise GitOperationError(
                f"git clone failed for {url}@{ref}: {exc2}"
            ) from exc
    except FileNotFoundError as exc:
        raise GitOperationError("git not found on PATH") from exc


def git_fetch_tags(repo: Path) -> None:
    try:
        _run(["git", "fetch", "--tags", "--prune", "--quiet"], cwd=repo)
    except RuntimeError as exc:
        raise GitOperationError(str(exc)) from exc


def git_current_ref(repo: Path) -> str:
    """Return ``<tag>`` if HEAD points at a tag, otherwise short sha."""
    tag = _run(
        ["git", "describe", "--tags", "--exact-match"], cwd=repo, check=False,
    )
    if tag.returncode == 0 and tag.stdout.strip():
        return tag.stdout.strip()
    sha = _run(["git", "rev-parse", "--short=12", "HEAD"], cwd=repo)
    return sha.stdout.strip()


def git_latest_tag(repo: Path) -> str | None:
    """The highest semver-like tag in the repo, or None if there are none.

    We rely on ``git tag --sort=-v:refname`` which sorts ``v0.10.0`` after
    ``v0.9.0`` (lexicographic would invert that). Pre-release tags like
    ``v0.2.0-rc1`` end up between the closest releases, which is what
    semver mandates.
    """
    out = _run(
        ["git", "tag", "--list", "v*", "--sort=-v:refname"], cwd=repo,
    )
    tags = [t.strip() for t in (out.stdout or "").splitlines() if t.strip()]
    return tags[0] if tags else None


def git_checkout(repo: Path, ref: str) -> None:
    try:
        _run(["git", "fetch", "--quiet"], cwd=repo)
        _run(["git", "checkout", ref], cwd=repo)
    except RuntimeError as exc:
        raise GitOperationError(str(exc)) from exc


def git_diff_changelog(repo: Path, from_ref: str, to_ref: str) -> str:
    """Diff of CHANGELOG.md between two refs. Empty string if either
    ref is missing or the file doesn't exist on either side."""
    if not (repo / "CHANGELOG.md").exists():
        return ""
    try:
        result = _run(
            ["git", "diff", f"{from_ref}..{to_ref}", "--", "CHANGELOG.md"],
            cwd=repo,
            check=False,
        )
    except RuntimeError:
        return ""
    return result.stdout or ""


# ---------------------------------------------------------------------------
# venv / pip
# ---------------------------------------------------------------------------


def create_venv(install_dir: Path, python_bin: str = "python3") -> Path:
    """Create ``<install_dir>/venv`` if it doesn't exist. Returns the
    venv's python binary path."""
    venv = install_dir / "venv"
    if venv.exists():
        return venv / "bin" / "python"
    try:
        _run([python_bin, "-m", "venv", str(venv)])
    except (RuntimeError, FileNotFoundError) as exc:
        raise VenvError(f"venv creation failed: {exc}") from exc
    return venv / "bin" / "python"


def pip_install(venv_python: Path, install_dir: Path) -> None:
    """Install the service's runtime requirements. Prefers
    ``requirements.lock`` if present (build-time hashes), else falls back
    to ``requirements.txt``."""
    lockfile = install_dir / "requirements.lock"
    reqfile = install_dir / "requirements.txt"
    if lockfile.exists():
        cmd = [str(venv_python), "-m", "pip", "install",
               "--require-hashes", "--no-deps", "-r", str(lockfile)]
    elif reqfile.exists():
        cmd = [str(venv_python), "-m", "pip", "install", "-r", str(reqfile)]
    else:
        raise VenvError(
            f"no requirements file at {reqfile} or {lockfile}"
        )
    try:
        _run(cmd, capture=False)
    except RuntimeError as exc:
        raise VenvError(f"pip install failed: {exc}") from exc
