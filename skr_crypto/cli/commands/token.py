"""``skr-crypto token ...`` — manage per-caller API tokens.

Subcommands:

  - ``list``        — list every token (active + optionally revoked)
  - ``show NAME``   — full metadata for one token (no plaintext)
  - ``create``      — mint a new token; prints the value ONCE
  - ``revoke ID``   — mark a token as revoked (immediate; in-flight
                       requests complete, then 401)
  - ``rotate ID``   — create a new token with the same scopes + revoke
                       the old one in a single transaction

Tokens are stored in the same SQLite DB as idempotency state, scoped
by ``IDEMPOTENCY_DB_PATH``. The CLI talks to that DB directly — no
running service required for `list` / `revoke` / `rotate`. (`create`
also works offline; the new token is picked up on next service
restart.)
"""
from __future__ import annotations

from pathlib import Path

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.exceptions import SkrCryptoError

# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group("token")
def group() -> None:
    """Manage per-caller API tokens with scopes (admin/send/read/metrics)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _token_store_path(ctx: click.Context) -> Path | None:
    """Resolve the token store path from the install dir's .env.

    Returns None when ``IDEMPOTENCY_DB_PATH`` is unset (in-memory mode
    — token CLI is not useful in that case, but `create` still works
    if the service hasn't started yet)."""
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    env = config.read_env_file(install_dir / ".env")
    raw = env.get("IDEMPOTENCY_DB_PATH", "")
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = install_dir / p
    return p


def _import_store():
    """Lazy import — needs the [server] extra (cryptography for scrypt)."""
    try:
        from skr_crypto.server import tokens as _tokens
    except ImportError as exc:
        raise SkrCryptoError(
            "token commands require `cryptography` — "
            "run `pip install 'skr-crypto[server]'`."
        ) from exc
    return _tokens


def _open_store(ctx: click.Context):
    """Build a TokenStore pointing at the install's SQLite DB."""
    store_mod = _import_store()
    path = _token_store_path(ctx)
    return store_mod.TokenStore(db_path=path)


# ---------------------------------------------------------------------------
# `token list`
# ---------------------------------------------------------------------------


@group.command("list")
@click.option(
    "--include-revoked", is_flag=True,
    help="Show revoked tokens too. Off by default.",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def list_cmd(ctx: click.Context, include_revoked: bool, as_json: bool) -> None:
    """List configured API tokens."""
    store = _open_store(ctx)
    rows = store.list(include_revoked=include_revoked)
    if as_json:
        output.print_json([
            {
                "id": r.id,
                "name": r.name,
                "scopes": sorted(r.scopes),
                "created_at": r.created_at,
                "last_used_at": r.last_used_at,
                "revoked_at": r.revoked_at,
                "created_by": r.created_by,
            }
            for r in rows
        ])
        return
    if not rows:
        output.warn(
            "No tokens configured. Mint one with "
            "`skr-crypto token create --name <label> --scope send,read`."
        )
        return
    table = output.make_table(
        "ID", "Name", "Scopes", "Created", "Last used", "Status",
        title="API tokens",
    )
    for r in rows:
        status = "revoked" if r.revoked_at else "active"
        table.add_row(
            r.id, r.name, ",".join(sorted(r.scopes)),
            r.created_at[:19],
            r.last_used_at[:19] if r.last_used_at else "—",
            status,
        )
    output.render_table(table)


# ---------------------------------------------------------------------------
# `token show ID`
# ---------------------------------------------------------------------------


@group.command("show")
@click.argument("token_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def show_cmd(ctx: click.Context, token_id: str, as_json: bool) -> None:
    """Show one token's metadata (no plaintext)."""
    store = _open_store(ctx)
    rec = store.get(token_id)
    if rec is None:
        raise SkrCryptoError(f"no token with id={token_id!r}")
    body = {
        "id": rec.id,
        "name": rec.name,
        "scopes": sorted(rec.scopes),
        "created_at": rec.created_at,
        "last_used_at": rec.last_used_at,
        "revoked_at": rec.revoked_at,
        "created_by": rec.created_by,
    }
    if as_json:
        output.print_json(body)
        return
    table = output.make_table("Field", "Value", title=f"Token {rec.id}")
    for k, v in body.items():
        table.add_row(k, str(v) if not isinstance(v, list) else ",".join(v))
    output.render_table(table)


# ---------------------------------------------------------------------------
# `token create`
# ---------------------------------------------------------------------------


@group.command("create")
@click.option(
    "--name", "-n", required=True,
    help="Operator-chosen label (e.g. 'backoffice-prod', 'metrics-prom').",
)
@click.option(
    "--scope", "-s", "scopes_str",
    default="send,read", show_default=True,
    help="Comma-separated scopes: admin / send / read / metrics. "
         "send implies read; admin implies everything.",
)
@click.pass_context
def create_cmd(ctx: click.Context, name: str, scopes_str: str) -> None:
    """Mint a new token. The plaintext value is shown ONCE."""
    store = _open_store(ctx)
    scopes = [s.strip() for s in scopes_str.split(",") if s.strip()]
    try:
        rec, plaintext = store.create(name=name, scopes=scopes)
    except Exception as exc:
        raise SkrCryptoError(f"failed to create token: {exc}") from exc
    output.success(f"Created token id={rec.id} name={name!r}")
    output.info(f"Scopes: {','.join(sorted(rec.scopes))}")
    output.info("")
    output.warn(
        "[bold]The token is shown ONCE — copy it now.[/bold]"
    )
    output.print_value(plaintext)
    output.info("")
    output.info(
        "Restart the service to load the token. Use it via "
        "`X-API-Key: <token>` header."
    )


# ---------------------------------------------------------------------------
# `token revoke ID`
# ---------------------------------------------------------------------------


@group.command("revoke")
@click.argument("token_id")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
@click.pass_context
def revoke_cmd(ctx: click.Context, token_id: str, yes: bool) -> None:
    """Mark a token as revoked. In-flight requests with this token
    complete; subsequent requests get 401."""
    store = _open_store(ctx)
    rec = store.get(token_id)
    if rec is None:
        raise SkrCryptoError(f"no token with id={token_id!r}")
    if rec.revoked_at:
        output.info(f"Token {token_id} is already revoked at {rec.revoked_at}.")
        return
    if not yes and not output.confirm(
        f"Revoke token {token_id} (name={rec.name!r})? Cannot be undone.",
        default=False,
    ):
        output.info("Aborted.")
        ctx.exit(1)
    store.revoke(token_id)
    output.success(f"Revoked token {token_id}")


# ---------------------------------------------------------------------------
# `token rotate ID` — create + revoke in one shot
# ---------------------------------------------------------------------------


@group.command("rotate")
@click.argument("token_id")
@click.option("--yes", "-y", is_flag=True)
@click.pass_context
def rotate_cmd(ctx: click.Context, token_id: str, yes: bool) -> None:
    """Mint a new token with the same name+scopes, revoke the old.

    Use when a token is suspected leaked but service mustn't go down.
    The two tokens coexist long enough for callers to swap their config.
    """
    store = _open_store(ctx)
    rec = store.get(token_id)
    if rec is None:
        raise SkrCryptoError(f"no token with id={token_id!r}")
    if rec.revoked_at:
        raise SkrCryptoError(
            f"token {token_id} already revoked — use create instead"
        )
    if not yes and not output.confirm(
        f"Rotate token {token_id} (name={rec.name!r})? "
        "A new token replaces it; the old one is revoked.",
        default=False,
    ):
        output.info("Aborted.")
        ctx.exit(1)
    new_rec, plaintext = store.create(
        name=rec.name,
        scopes=sorted(rec.scopes),
        created_by=token_id,
    )
    store.revoke(token_id)
    output.success(
        f"Rotated: revoked id={token_id}, created id={new_rec.id}"
    )
    output.warn("[bold]The new token is shown ONCE — copy it now.[/bold]")
    output.print_value(plaintext)
