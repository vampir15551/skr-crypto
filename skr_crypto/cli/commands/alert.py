"""``skr-crypto alert ...`` — manage opt-in Telegram operator alerts (1.9.0+).

Subcommands:

  - ``setup``           — walk the operator through bot/chat_id config
                          and print the .env block to add (no auto-edit)
  - ``test``            — POST a synthetic alert via Telegram Bot API
                          to verify wiring without waiting for a real event
  - ``list-deliveries`` — recent delivery attempts (status, code, retries)
  - ``retry ID``        — force-retry a giving_up / failed delivery

See ADR 0013 for the full design.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import click

from skr_crypto.cli import config, output
from skr_crypto.cli.exceptions import SkrCryptoError


@click.group("alert")
def group() -> None:
    """Opt-in Telegram alerts for human operators (1.9.0+)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_store(ctx: click.Context):
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    env = config.read_env_file(install_dir / ".env")
    raw = env.get("IDEMPOTENCY_DB_PATH", "")
    if not raw:
        raise SkrCryptoError(
            "IDEMPOTENCY_DB_PATH is not set; alerts require persistent "
            "storage. Configure it in .env first."
        )
    db_path = Path(raw)
    if not db_path.is_absolute():
        db_path = install_dir / db_path
    try:
        from skr_crypto.server import alerts as alerts_mod
    except ImportError as exc:
        raise SkrCryptoError(
            "alerts require the [server] extras. "
            "Run `pip install 'skr-crypto[server]'`."
        ) from exc
    return alerts_mod.AlertDeliveryStore(db_path=str(db_path))


# ---------------------------------------------------------------------------
# `alert setup` — guided configuration
# ---------------------------------------------------------------------------


_BOTFATHER_HELP = """\
[bold]Setting up a Telegram bot:[/bold]

  1. Open Telegram, message [italic]@BotFather[/italic].
  2. Send [italic]/newbot[/italic]; pick a name + username.
  3. Copy the HTTP API token BotFather returns
     (looks like [italic]123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11[/italic]).

[bold]Picking a chat_id:[/bold]

  - For a personal DM: open the bot, send [italic]/start[/italic],
    then forward any of your messages to [italic]@userinfobot[/italic]
    — it replies with your numeric user id.
  - For a group: add the bot to the group, send a message, then
    open [italic]https://api.telegram.org/bot<TOKEN>/getUpdates[/italic]
    and copy the negative-prefixed [italic]chat.id[/italic].
  - For a channel: add the bot as admin, post once, then
    [italic]getUpdates[/italic] as above.
"""


@group.command("setup")
def setup_cmd() -> None:
    """Print Telegram setup instructions + the .env block to add.

    Does NOT modify .env — keeps the operator in control of secrets.
    Walks through @BotFather, chat_id resolution, and the env vars.
    """
    output.info(_BOTFATHER_HELP)
    output.info("")
    output.info("[bold]Add to your .env (or your secret manager):[/bold]")
    output.info("")
    output.print_value("TELEGRAM_BOT_TOKEN=<paste-the-token-from-BotFather>")
    output.print_value("TELEGRAM_CHAT_ID=<paste-the-numeric-chat-id>")
    output.info("")
    output.info("Optional knobs (defaults shown):")
    output.print_value("ALERT_EVENTS=SEND_REJECTED,SEND_FAILED,WEBHOOK_GIVEUP")
    output.print_value("ALERT_SEND_THRESHOLD_USDT=          # e.g. 5000 to alert on big sends")
    output.print_value("ALERT_SANCTIONS_HIT_NOTIFY=true")
    output.print_value("ALERT_QUIET_HOURS_UTC=              # e.g. 22-08 (UTC)")
    output.print_value("ALERT_BACKOFF_SCHEDULE=0,5,30,300")
    output.print_value("ALERT_TIMEOUT_SEC=10")
    output.info("")
    output.info(
        "After [italic]restarting[/italic] the service, verify wiring with "
        "[italic]skr-crypto alert test[/italic]."
    )


# ---------------------------------------------------------------------------
# `alert test` — synthetic Bot API call
# ---------------------------------------------------------------------------


@group.command("test")
@click.option("--bot-token", default=None,
              help="Override TELEGRAM_BOT_TOKEN from .env.")
@click.option("--chat-id", default=None,
              help="Override TELEGRAM_CHAT_ID from .env.")
@click.option("--timeout", default=10.0, show_default=True, type=float)
@click.pass_context
def test_cmd(
    ctx: click.Context, bot_token: str | None, chat_id: str | None,
    timeout: float,
) -> None:
    """POST a synthetic alert to the configured Telegram chat.

    Uses the live Bot API — verifies bot token + chat_id + network
    reachability end-to-end without waiting for a real audit event.
    The synthetic message is clearly marked so receivers can ignore.
    """
    if not bot_token or not chat_id:
        install_dir = config.require_installed(ctx.obj.get("install_dir"))
        env = config.read_env_file(install_dir / ".env")
        bot_token = bot_token or env.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = chat_id or env.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        raise SkrCryptoError(
            "no Telegram bot token / chat_id available. Pass --bot-token "
            "and --chat-id, or run `skr-crypto alert setup` and add "
            "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to .env."
        )
    try:
        from skr_crypto.server import alerts as alerts_mod
    except ImportError as exc:
        raise SkrCryptoError("alerts require [server] extras") from exc

    fake_entry = {
        "id": "synthetic-test",
        "event": "ALERT_TEST",
        "timestamp": datetime.now(UTC).isoformat(),
        "wallet": "test",
        "to_address": "TXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "amount": "1.00",
        "asset": "USDT",
        "idempotency_key": "synthetic-key",
        "client_ip": "127.0.0.1",
        "token_id": "synthetic",
        "result": "test",
        "details": "synthetic test alert from skr-crypto alert test",
    }
    message = alerts_mod.format_message(fake_entry)

    output.info("POSTing synthetic ALERT_TEST to api.telegram.org…")
    started = time.time()
    try:
        code, body = alerts_mod.telegram_send_message(
            bot_token, chat_id, message, timeout_sec=timeout,
        )
    except Exception as exc:
        raise SkrCryptoError(f"delivery failed: {exc}") from exc
    elapsed = time.time() - started

    if 200 <= code < 300:
        output.success(
            f"Telegram returned {code} in {elapsed:.2f}s. Check the chat — "
            f"the synthetic message should be visible there."
        )
    else:
        raise SkrCryptoError(
            f"Telegram returned {code}: {body[:300]}\n"
            f"Common causes: wrong bot token (401), bot not added to the "
            f"chat (403), wrong chat_id (400 chat not found)."
        )


# ---------------------------------------------------------------------------
# `alert list-deliveries`
# ---------------------------------------------------------------------------


@group.command("list-deliveries")
@click.option("--limit", default=50, show_default=True, type=int,
              help="Max rows (most recent first).")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def list_deliveries_cmd(ctx: click.Context, limit: int, as_json: bool) -> None:
    """Show recent Telegram alert delivery attempts."""
    store = _open_store(ctx)
    rows = store.list_recent(limit=limit)
    store.close()
    if as_json:
        output.print_json([
            {
                "id": d.id, "event": d.event, "chat_id": d.chat_id,
                "status": d.status, "attempts": d.attempts,
                "last_response_code": d.last_response_code,
                "last_error": d.last_error,
                "created_at": d.created_at,
            }
            for d in rows
        ])
        return
    if not rows:
        output.warn("No alert deliveries yet.")
        return
    table = output.make_table(
        "ID", "Event", "Chat", "Status", "Attempts", "Code", "Error",
        title="Telegram alert deliveries (most recent first)",
    )
    for d in rows:
        chat_short = d.chat_id if len(d.chat_id) <= 18 else d.chat_id[:15] + "..."
        err_short = (d.last_error or "")[:30]
        table.add_row(
            str(d.id), d.event, chat_short, d.status,
            str(d.attempts),
            str(d.last_response_code) if d.last_response_code else "-",
            err_short,
        )
    output.render_table(table)


# ---------------------------------------------------------------------------
# `alert retry ID`
# ---------------------------------------------------------------------------


@group.command("retry")
@click.argument("delivery_id", type=int)
@click.pass_context
def retry_cmd(ctx: click.Context, delivery_id: int) -> None:
    """Reset a giving_up / failed alert back to pending for one more try."""
    store = _open_store(ctx)
    rec = store.get(delivery_id)
    if rec is None:
        store.close()
        raise SkrCryptoError(f"no alert delivery with id={delivery_id}")
    if rec.status not in ("giving_up", "failed"):
        store.close()
        raise SkrCryptoError(
            f"alert delivery id={delivery_id} is in state {rec.status!r}; "
            f"only giving_up / failed alerts can be retried"
        )
    if not store.force_retry(delivery_id):
        store.close()
        raise SkrCryptoError(f"could not reset alert delivery id={delivery_id}")
    store.close()
    output.success(
        f"Alert delivery id={delivery_id} reset to pending; the worker "
        f"will pick it up on its next tick (~1s)."
    )
