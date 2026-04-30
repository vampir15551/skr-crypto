"""Click root group + entry point.

Each subcommand lives in ``skr_crypto.commands.*`` and is attached
here. Commands are imported lazily inside ``_register`` to keep
``skr-crypto --help`` fast even as the codebase grows.

Error-to-exit-code translation is delegated to click's machinery:
``SkrCryptoError`` is a ``click.ClickException`` subclass with a typed
``exit_code``. Click's standard handler honours it both for the
installed entry point and for ``click.testing.CliRunner``.
"""
from __future__ import annotations

import click

from skr_crypto.version import __version__


@click.group(
    context_settings={
        "help_option_names": ["-h", "--help"],
        "max_content_width": 100,
    },
    help=(
        "Operator CLI for the SKR Crypto microservice fleet.\n\n"
        "All commands are read-only or operational (install / update / "
        "restart / inspect). Money-moving operations go through the "
        "service's authenticated HTTP API and are deliberately NOT "
        "exposed here. See SECURITY.md."
    ),
)
@click.version_option(__version__, "-V", "--version", prog_name="skr-crypto")
@click.option(
    "--dir", "install_dir",
    envvar="SKR_CRYPTO_HOME",
    type=click.Path(file_okay=False),
    help="Install dir (default: ~/.skr-crypto, or $SKR_CRYPTO_HOME).",
)
@click.option(
    "-q", "--quiet", is_flag=True, default=False,
    help="Suppress informational and success lines on stderr. "
         "Errors and warnings still print. Useful for CI / scripts.",
)
@click.pass_context
def cli(ctx: click.Context, install_dir: str | None, quiet: bool) -> None:
    ctx.ensure_object(dict)
    ctx.obj["install_dir"] = install_dir
    ctx.obj["quiet"] = quiet
    if quiet:
        # output.info / output.success route through this flag.
        from skr_crypto.cli import output
        output.set_quiet(True)


def _register() -> None:
    """Lazy import + attach each subcommand. Keeps `--help` quick and
    isolates import failures: a broken command can't crash the whole CLI."""
    from skr_crypto.cli.commands import (
        audit,
        backup,
        balance,
        check_tx,
        completion,
        config_cmd,
        doctor,
        help_cmd,
        install,
        keygen,
        logs,
        reconcile,
        restore,
        risk,
        start_stop,
        status,
        token,
        update,
        version_cmd,
        wallet,
    )

    cli.add_command(install.cmd)
    cli.add_command(update.cmd)
    cli.add_command(status.cmd)
    cli.add_command(start_stop.start_cmd)
    cli.add_command(start_stop.stop_cmd)
    cli.add_command(start_stop.restart_cmd)
    cli.add_command(logs.cmd)
    cli.add_command(balance.cmd)
    cli.add_command(check_tx.cmd)
    cli.add_command(risk.cmd)
    cli.add_command(audit.cmd)
    cli.add_command(reconcile.cmd)
    cli.add_command(config_cmd.config_group)
    cli.add_command(keygen.cmd)
    cli.add_command(wallet.group)
    cli.add_command(token.group)
    cli.add_command(backup.cmd)
    cli.add_command(restore.cmd)
    cli.add_command(doctor.cmd)
    cli.add_command(completion.cmd)
    cli.add_command(version_cmd.cmd)
    cli.add_command(help_cmd.cmd)


_register()


def main() -> None:
    """Console-script entry point. Click handles the exception →
    exit-code mapping for us (SkrCryptoError extends ClickException)."""
    cli()


if __name__ == "__main__":
    main()
