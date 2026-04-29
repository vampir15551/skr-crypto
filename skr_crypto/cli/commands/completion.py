"""``skr-crypto completion <shell>`` — print a shell-completion script.

Click 8 ships with built-in completion. The convention is to set an
env var like ``_SKR_CRYPTO_COMPLETE=zsh_source skr-crypto`` and source
the result. We expose this as a friendly subcommand so operators
don't have to remember the env-var dance.

Usage:

    skr-crypto completion bash >> ~/.bashrc
    skr-crypto completion zsh  >> ~/.zshrc
    skr-crypto completion fish > ~/.config/fish/completions/skr-crypto.fish

Then restart the shell. Tab-completion now works for every command,
flag, and option.
"""
from __future__ import annotations

import os
import subprocess
import sys

import click

from skr_crypto.cli import output
from skr_crypto.cli.exceptions import SkrCryptoError

SHELLS = ("bash", "zsh", "fish")


@click.command("completion")
@click.argument("shell", type=click.Choice(SHELLS, case_sensitive=False))
def cmd(shell: str) -> None:
    """Print a shell-completion script for the chosen shell.

    Pipe the output into your shell's startup file, e.g.

        skr-crypto completion zsh >> ~/.zshrc

    Re-run after upgrading skr-crypto so newly-added commands also tab-complete.
    """
    shell = shell.lower()

    # Click's mechanism: set _<APP_NAME>_COMPLETE=<shell>_source and
    # invoke the binary; it prints the completion script. We just
    # forward to a clean subprocess so STDOUT is exactly what the user
    # would otherwise pipe in directly.
    env = os.environ.copy()
    env["_SKR_CRYPTO_COMPLETE"] = f"{shell}_source"

    # Click's completion mechanism is keyed off the program name from
    # argv[0]; the env var must match. Use the binary that's running
    # us right now (if argv[0] ends with skr-crypto) so we work even
    # when the venv isn't on PATH. Fall back to PATH lookup.
    import shutil as _shutil
    binary = sys.argv[0] if sys.argv and sys.argv[0].endswith("skr-crypto") else None
    if not binary:
        binary = _shutil.which("skr-crypto")
    if not binary:
        raise SkrCryptoError(
            "could not find a `skr-crypto` binary to introspect — completion "
            "needs the console-script entry point. Install via "
            "`pip install skr-crypto` and try again."
        )

    try:
        result = subprocess.run(
            [binary],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover
        raise SkrCryptoError(f"could not invoke {binary}: {exc}") from exc

    # Click's completion writes to stdout when invoked this way; on
    # success rc is 0 and stdout has the script.
    if result.returncode != 0:
        raise SkrCryptoError(
            f"click failed to emit completion script: "
            f"{(result.stderr or '').strip() or 'no stderr'}"
        )
    if not result.stdout.strip():
        raise SkrCryptoError(
            f"click returned an empty completion script for {shell!r} — "
            f"is your click version too old?"
        )

    sys.stdout.write(result.stdout)
    if not result.stdout.endswith("\n"):
        sys.stdout.write("\n")

    rcfile = {
        "bash": "~/.bashrc",
        "zsh":  "~/.zshrc",
        "fish": "~/.config/fish/completions/skr-crypto.fish",
    }[shell]
    output.info(
        f"# Source the above into your shell's startup file. Example:\n"
        f"#   skr-crypto completion {shell} >> {rcfile}"
    )
