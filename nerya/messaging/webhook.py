"""Generic webhook channel — JSON POST to any HTTP endpoint.

Config shape in ``workspace/messages/channels.yml``:

    channels:
      ops:
        kind: webhook
        url: https://example.com/hooks/ops         # either this
        url_ref: vault://ops_webhook_url            # or vault-backed
        headers: {"X-Source": "nerya"}              # optional
        auth_header_ref: vault://ops_bearer         # optional bearer
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
    message["channel"] = "webhook"
    cfg = channel_cfg or {}
    url = cfg.get("url")
    if not url and cfg.get("url_ref"):
        url = _resolve(cfg["url_ref"], resolve_secret)
    if not url:
        message["delivered"] = False
        message["delivery_note"] = "webhook: missing url / url_ref"
        return dashboard.send(outbox_messages, message)

    headers = dict(cfg.get("headers") or {})
    if cfg.get("auth_header_ref"):
        secret = _resolve(cfg["auth_header_ref"], resolve_secret)
        if secret:
            headers.setdefault("Authorization", f"Bearer {secret}")

    tx = transport or UrllibMessagingTransport()
    body = {
        "message_id": message.get("message_id"),
        "channel": message.get("channel"),
        "strategy_id": message.get("strategy_id"),
        "ts": message.get("ts"),
        "text": message.get("text"),
    }
    status, resp = tx.post(url, headers=headers, body=body, timeout=10.0)
    ok = 200 <= status < 300
    message["delivered"] = ok
    message["status"] = status
    message["delivery_note"] = "webhook sent" if ok else f"webhook failed: {resp}"
    return dashboard.send(outbox_messages, message)


def _resolve(ref: str | None, resolver: Callable[[str], str | None] | None) -> str | None:
    if not ref:
        return None
    if resolver is None:
        return None
    return resolver(ref)
