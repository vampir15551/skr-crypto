"""``skr-crypto webhook ...`` — manage opt-in webhook delivery (1.7.0+).

Subcommands:

  - ``setup``              — generate a fresh signing secret + print the
                             .env entries to add (no automatic edit)
  - ``test URL``           — POST a synthetic event to URL with a real
                             signature. Verify the receiver wiring without
                             waiting for a real /send.
  - ``list-deliveries``    — recent delivery attempts (status, code, retries)
  - ``retry ID``           — force-retry a giving_up / failed delivery

See ADR 0011 for the full design + receiver verification spec.
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

import click
import requests

from skr_crypto.cli import config, output
from skr_crypto.cli.exceptions import SkrCryptoError

# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group("webhook")
def group() -> None:
    """Opt-in webhook delivery for audit events (1.7.0+)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_store(ctx: click.Context):
    """Open the webhook_deliveries store at the install's idempotency DB."""
    install_dir = config.require_installed(ctx.obj.get("install_dir"))
    env = config.read_env_file(install_dir / ".env")
    raw = env.get("IDEMPOTENCY_DB_PATH", "")
    if not raw:
        raise SkrCryptoError(
            "IDEMPOTENCY_DB_PATH is not set; webhooks require persistent "
            "storage. Configure it in .env first."
        )
    db_path = Path(raw)
    if not db_path.is_absolute():
        db_path = install_dir / db_path
    try:
        from skr_crypto.server import webhooks as wh_mod
    except ImportError as exc:
        raise SkrCryptoError(
            "webhooks require the [server] extras (requests, cryptography). "
            "Run `pip install 'skr-crypto[server]'`."
        ) from exc
    return wh_mod.WebhookDeliveryStore(db_path=str(db_path))


# ---------------------------------------------------------------------------
# `webhook setup`
# ---------------------------------------------------------------------------


@group.command("setup")
def setup_cmd() -> None:
    """Generate a fresh HMAC signing secret + print .env entries.

    The secret is printed to stdout. Copy it into ``.env`` as
    ``WEBHOOK_SIGNING_SECRET=...`` then add ``WEBHOOK_URLS=...`` for
    each receiver. Restart the service after the change.
    """
    secret = secrets.token_hex(32)  # 64 hex chars = 256 bits
    output.success("Generated a fresh HMAC-SHA256 signing secret.")
    output.info("")
    output.info("[bold]Add these lines to your `.env` (or your secret manager):[/bold]")
    output.info("")
    output.print_value(f"WEBHOOK_SIGNING_SECRET={secret}")
    output.print_value("WEBHOOK_URLS=https://your-receiver.example.com/webhook")
    output.info("")
    output.info("Optional knobs (defaults shown):")
    output.print_value("WEBHOOK_EVENTS=SEND_SUCCESS,SEND_REJECTED,SEND_FAILED,SEND_DUPLICATE,RECEIPT_RESOLVED")
    output.print_value("WEBHOOK_BACKOFF_SCHEDULE=0,30,120,600,3600")
    output.print_value("WEBHOOK_TIMEOUT_SEC=10")
    output.print_value("WEBHOOK_TIMESTAMP_TOLERANCE_SEC=300")
    output.info("")
    output.info(
        "After restart, the receiver MUST verify X-SKR-Signature using "
        "the same secret. See ADR 0011 for the 5-line verification spec."
    )


# ---------------------------------------------------------------------------
# `webhook test URL`
# ---------------------------------------------------------------------------


@group.command("test")
@click.argument("url")
@click.option(
    "--secret", default=None,
    help="HMAC secret. Defaults to WEBHOOK_SIGNING_SECRET in the install's .env.",
)
@click.option("--timeout", default=10.0, show_default=True, type=float)
@click.pass_context
def test_cmd(ctx: click.Context, url: str, secret: str | None, timeout: float) -> None:
    """POST a synthetic SEND_SUCCESS event to URL with a real signature.

    Use to verify the receiver's wiring + signature verification end-to-end
    without waiting for a real transfer. The synthetic payload is clearly
    marked ``"synthetic": true`` so the receiver can ignore it on accept.
    """
    if not secret:
        install_dir = config.require_installed(ctx.obj.get("install_dir"))
        env = config.read_env_file(install_dir / ".env")
        secret = env.get("WEBHOOK_SIGNING_SECRET", "")
    if not secret:
        raise SkrCryptoError(
            "no signing secret available. Pass --secret or run "
            "`skr-crypto webhook setup` and add WEBHOOK_SIGNING_SECRET to .env."
        )

    try:
        from skr_crypto.server import webhooks as wh_mod
    except ImportError as exc:
        raise SkrCryptoError("webhooks require [server] extras") from exc

    payload_dict = {
        "synthetic": True,
        "id": "test-delivery-0",
        "event": "SEND_SUCCESS",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "wallet": "test",
        "from_address": "TTestxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "to_address": "TXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "amount": "1.00",
        "asset": "USDT",
        "txid": "synthetic-test-tx-id",
        "idempotency_key": "synthetic-key",
        "client_ip": "127.0.0.1",
        "token_id": "synthetic",
        "result": "broadcast",
    }
    body = json.dumps(payload_dict)
    ts = int(time.time())
    sig = wh_mod.sign_payload(secret, ts, body)

    output.info(f"POSTing synthetic SEND_SUCCESS to {url}")
    try:
        resp = requests.post(
            url, data=body,
            headers={
                "Content-Type": "application/json",
                "X-SKR-Signature": sig,
                "X-SKR-Event": "SEND_SUCCESS",
                "X-SKR-Delivery-Id": "synthetic",
                "User-Agent": "skr-crypto-webhook/1 (test)",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SkrCryptoError(f"delivery failed: {exc}") from exc

    if 200 <= resp.status_code < 300:
        output.success(
            f"Receiver returned {resp.status_code}. Response body: "
            f"{resp.text[:200]!r}"
        )
        output.info("Wiring looks good. The receiver accepted the synthetic event.")
    else:
        raise SkrCryptoError(
            f"Receiver returned {resp.status_code}: {resp.text[:300]}\n"
            f"Common causes: signature verification failed (wrong secret?), "
            f"event filter too narrow, receiver returned non-2xx for valid input."
        )


# ---------------------------------------------------------------------------
# `webhook list-deliveries`
# ---------------------------------------------------------------------------


@group.command("list-deliveries")
@click.option(
    "--limit", default=50, show_default=True, type=int,
    help="Max rows to show (most recent first).",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def list_deliveries_cmd(ctx: click.Context, limit: int, as_json: bool) -> None:
    """Show recent webhook delivery attempts."""
    store = _open_store(ctx)
    rows = store.list_recent(limit=limit)
    store.close()
    if as_json:
        output.print_json([
            {
                "id": d.id, "event": d.event, "url": d.url,
                "status": d.status, "attempts": d.attempts,
                "last_response_code": d.last_response_code,
                "last_error": d.last_error,
                "created_at": d.created_at,
            }
            for d in rows
        ])
        return
    if not rows:
        output.warn("No webhook deliveries yet.")
        return
    table = output.make_table(
        "ID", "Event", "URL", "Status", "Attempts", "Code", "Error",
        title="Webhook deliveries (most recent first)",
    )
    for d in rows:
        url_short = d.url if len(d.url) <= 40 else d.url[:37] + "..."
        err_short = (d.last_error or "")[:30]
        table.add_row(
            str(d.id), d.event, url_short, d.status,
            str(d.attempts),
            str(d.last_response_code) if d.last_response_code else "-",
            err_short,
        )
    output.render_table(table)


# ---------------------------------------------------------------------------
# `webhook retry ID`
# ---------------------------------------------------------------------------


@group.command("retry")
@click.argument("delivery_id", type=int)
@click.pass_context
def retry_cmd(ctx: click.Context, delivery_id: int) -> None:
    """Reset a giving_up / failed delivery back to pending for one more try."""
    store = _open_store(ctx)
    rec = store.get(delivery_id)
    if rec is None:
        store.close()
        raise SkrCryptoError(f"no delivery with id={delivery_id}")
    if rec.status not in ("giving_up", "failed"):
        store.close()
        raise SkrCryptoError(
            f"delivery id={delivery_id} is in state {rec.status!r}; "
            f"only giving_up / failed deliveries can be retried"
        )
    if not store.force_retry(delivery_id):
        store.close()
        raise SkrCryptoError(f"could not reset delivery id={delivery_id}")
    store.close()
    output.success(
        f"Delivery id={delivery_id} reset to pending; the worker will pick it "
        f"up on its next tick (~1s)."
    )
