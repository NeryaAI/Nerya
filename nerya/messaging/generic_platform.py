"""Webhook-backed sender for Hermes-aligned gateway platforms."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import dashboard
from .transport import MessagingTransport, UrllibMessagingTransport


def send(outbox_messages: Path, message: dict[str, Any], *,
         channel_cfg: dict[str, Any] | None = None,
         resolve_secret: Callable[[str], str | None] | None = None,
         transport: MessagingTransport | None = None) -> Path:
    cfg = channel_cfg or {}
    platform = str(cfg.get("kind") or message.get("kind") or message.get("channel") or "webhook")
    message["channel"] = platform
    url = _resolve_url(cfg, resolve_secret)
    if not url:
        message["delivered"] = False
        message["delivery_note"] = f"{platform}: missing webhook_url/url or *_ref"
        return dashboard.send(outbox_messages, message)

    tx = transport or UrllibMessagingTransport()
    headers = dict(cfg.get("headers") or {})
    body = _body_for(platform, message, cfg)
    status, resp = tx.post(url, headers=headers, body=body, timeout=float(cfg.get("timeout_s") or 10.0))
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    message["delivered"] = ok
    message["status"] = status
    message["delivery_note"] = f"{platform} sent" if ok else f"{platform} failed: {resp}"
    return dashboard.send(outbox_messages, message)


def send_status(*, channel_cfg: dict[str, Any] | None = None,
                text: str,
                resolve_secret: Callable[[str], str | None] | None = None,
                transport: MessagingTransport | None = None) -> dict[str, Any]:
    cfg = channel_cfg or {}
    url = cfg.get("status_webhook_url") or cfg.get("status_url")
    if not url and cfg.get("status_webhook_url_ref"):
        url = _resolve(cfg.get("status_webhook_url_ref"), resolve_secret)
    if not url:
        return {"ok": False, "error": "missing status_webhook_url"}
    tx = transport or UrllibMessagingTransport()
    status, resp = tx.post(
        str(url), headers=dict(cfg.get("status_headers") or {}),
        body={"type": "typing", "text": text, "platform": cfg.get("kind")},
        timeout=float(cfg.get("timeout_s") or 10.0),
    )
    return {"ok": 200 <= status < 300 and bool(resp.get("ok", True)), "status": status, "response": resp}


def _resolve_url(cfg: dict[str, Any], resolver: Callable[[str], str | None] | None) -> str | None:
    for key in ("webhook_url", "url", "incoming_webhook_url"):
        if cfg.get(key):
            return str(cfg[key])
    for key in ("webhook_url_ref", "url_ref", "incoming_webhook_url_ref"):
        ref = cfg.get(key)
        if ref:
            resolved = _resolve(str(ref), resolver)
            if resolved:
                return resolved
    return None


def _resolve(ref: str | None, resolver: Callable[[str], str | None] | None) -> str | None:
    if not ref or resolver is None:
        return None
    return resolver(ref)


def _body_for(platform: str, message: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    text = message.get("text") or ""
    buttons = message.get("buttons") if isinstance(message.get("buttons"), list) else []
    event = message.get("event") if isinstance(message.get("event"), dict) else None
    normalized = platform.lower()
    if normalized in {"slack", "mattermost"}:
        body: dict[str, Any] = {"text": text}
        if buttons:
            body["buttons"] = buttons
        if message.get("approval_id"):
            body["approval_id"] = message.get("approval_id")
        return body
    if normalized in {"discord"}:
        body: dict[str, Any] = {"content": text}
        if buttons:
            body["buttons"] = buttons
        if message.get("approval_id"):
            body["approval_id"] = message.get("approval_id")
        if cfg.get("username"):
            body["username"] = cfg["username"]
        if cfg.get("avatar_url"):
            body["avatar_url"] = cfg["avatar_url"]
        return body
    if normalized in {"dingtalk"}:
        return {"msgtype": "text", "text": {"content": text}}
    if normalized in {"feishu", "wecom", "weixin", "qqbot"}:
        return {"msg_type": "text", "content": {"text": text}}
    return {
        "message_id": message.get("message_id"),
        "platform": platform,
        "channel": message.get("channel"),
        "strategy_id": message.get("strategy_id"),
        "ts": message.get("ts"),
        "text": text,
        "approval_id": message.get("approval_id"),
        "buttons": buttons,
        "event": message.get("event"),
        "approval": event,
    }
