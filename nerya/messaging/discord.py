"""Discord channel — delivery via webhook URL.

Config shape in ``workspace/messages/channels.yml``:

    channels:
      alerts:
        kind: discord
        webhook_url_ref: vault://discord_webhook_url  # required
        username: Nerya                                # optional
        avatar_url: https://...                        # optional
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import dashboard
from .transport import MessagingTransport, UrllibMessagingTransport


def send(outbox_messages: Path, message: dict[str, Any], *,
         channel_cfg: dict[str, Any] | None = None,
         resolve_secret: Callable[[str], str | None] | None = None,
         transport: MessagingTransport | None = None) -> Path:
    message["channel"] = "discord"
    cfg = channel_cfg or {}
    url_ref = cfg.get("webhook_url_ref") or cfg.get("url_ref")
    webhook_url = _resolve(url_ref, resolve_secret) if url_ref else cfg.get("webhook_url")

    if not webhook_url:
        message["delivered"] = False
        message["delivery_note"] = "discord: missing webhook_url / webhook_url_ref"
        return dashboard.send(outbox_messages, message)

    tx = transport or UrllibMessagingTransport()
    body: dict[str, Any] = {"content": message.get("text") or ""}
    if "username" in cfg:
        body["username"] = cfg["username"]
    if "avatar_url" in cfg:
        body["avatar_url"] = cfg["avatar_url"]

    status, resp = tx.post(webhook_url, headers={}, body=body, timeout=10.0)
    ok = 200 <= status < 300
    message["delivered"] = ok
    message["status"] = status
    message["delivery_note"] = "discord sent" if ok else f"discord failed: {resp}"
    return dashboard.send(outbox_messages, message)


def _resolve(ref: str | None, resolver: Callable[[str], str | None] | None) -> str | None:
    if not ref:
        return None
    if resolver is None:
        return None
    return resolver(ref)
