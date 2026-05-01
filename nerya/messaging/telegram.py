"""Telegram channel — real delivery via the Bot API.

Config shape in ``workspace/messages/channels.yml``:

    channels:
      alerts:
        kind: telegram
        bot_token_ref: vault://telegram_bot_token   # required for live delivery
        chat_id: "123456789"                          # required
        parse_mode: HTML                              # optional
        disable_web_page_preview: true                # optional

If ``bot_token_ref`` (or the resolved secret) is missing the message is
still written to the outbox as a record, but marked ``delivered: false``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import dashboard
from .markdown_telegram import render_markdown_for_telegram
from .transport import MessagingTransport, UrllibMessagingTransport


_API = "https://api.telegram.org/bot{token}/sendMessage"
_SET_COMMANDS_API = "https://api.telegram.org/bot{token}/setMyCommands"
_GET_UPDATES_API = "https://api.telegram.org/bot{token}/getUpdates"
_CHAT_ACTION_API = "https://api.telegram.org/bot{token}/sendChatAction"
_ANSWER_CALLBACK_API = "https://api.telegram.org/bot{token}/answerCallbackQuery"
_EDIT_REPLY_MARKUP_API = "https://api.telegram.org/bot{token}/editMessageReplyMarkup"


def send(outbox_messages: Path, message: dict[str, Any], *,
         channel_cfg: dict[str, Any] | None = None,
         resolve_secret: Callable[[str], str | None] | None = None,
         transport: MessagingTransport | None = None) -> Path:
    message["channel"] = "telegram"
    cfg = channel_cfg or {}
    token_ref = cfg.get("bot_token_ref") or cfg.get("token_ref")
    chat_id = cfg.get("chat_id")
    token = _resolve(token_ref, resolve_secret) if token_ref else None

    if not token or not chat_id:
        message["delivered"] = False
        message["delivery_note"] = "telegram: missing bot_token_ref or chat_id"
        return dashboard.send(outbox_messages, message)

    tx = transport or UrllibMessagingTransport()
    raw_text = message.get("text") or ""
    # Apr-27 2026 — render the agent's markdown into Telegram-friendly
    # HTML by default. Operators can opt out by setting
    # ``parse_mode: ""`` (force plain text), or pin a different mode
    # (e.g. ``Markdown`` / ``MarkdownV2``) in the channel config and
    # disable the converter via ``markdown: false``.
    parse_mode = cfg.get("parse_mode", "HTML")
    use_markdown_converter = bool(cfg.get("markdown", True)) and (
        str(parse_mode or "").upper() == "HTML"
    )
    text = (
        render_markdown_for_telegram(raw_text)
        if use_markdown_converter else raw_text
    )
    body: dict[str, Any] = {"chat_id": str(chat_id), "text": text}
    if parse_mode:
        body["parse_mode"] = parse_mode
    if "disable_web_page_preview" in cfg:
        body["disable_web_page_preview"] = bool(cfg["disable_web_page_preview"])
    # Plan 21 P0 §2 — inline approval keyboards. Callers can attach a
    # ``reply_markup`` directly on the message envelope; the messaging
    # pipeline will set it via ``approval_prompts.ApprovalPrompt``.
    reply_markup = message.get("reply_markup")
    if isinstance(reply_markup, dict) and reply_markup:
        body["reply_markup"] = reply_markup
    reply_to = message.get("reply_to_message_id")
    if reply_to is not None:
        body["reply_to_message_id"] = int(reply_to)

    status, resp = tx.post(_API.format(token=token), headers={}, body=body, timeout=10.0)
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    message["delivered"] = ok
    message["status"] = status
    message["platform_response"] = resp
    if isinstance(resp.get("result"), dict) and resp["result"].get("message_id") is not None:
        message["telegram_message_id"] = resp["result"].get("message_id")
    message["delivery_note"] = "telegram sent" if ok else f"telegram failed: {resp}"
    return dashboard.send(outbox_messages, message)


def _resolve(ref: str | None, resolver: Callable[[str], str | None] | None) -> str | None:
    if not ref:
        return None
    if resolver is None:
        return None
    return resolver(ref)


def _token(cfg: dict[str, Any], resolver: Callable[[str], str | None] | None) -> str | None:
    token_ref = cfg.get("bot_token_ref") or cfg.get("token_ref")
    return _resolve(token_ref, resolver) if token_ref else None


def set_commands(*, channel_cfg: dict[str, Any] | None = None,
                 commands: list[dict[str, str]] | None = None,
                 resolve_secret: Callable[[str], str | None] | None = None,
                 transport: MessagingTransport | None = None) -> dict[str, Any]:
    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref"}
    tx = transport or UrllibMessagingTransport()
    body = {"commands": commands or []}
    status, resp = tx.post(_SET_COMMANDS_API.format(token=token), headers={}, body=body, timeout=10.0)
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    return {"ok": ok, "status": status, "response": resp}


def get_updates(*, channel_cfg: dict[str, Any] | None = None,
                offset: int | None = None,
                limit: int = 10,
                timeout: int = 0,
                resolve_secret: Callable[[str], str | None] | None = None,
                transport: MessagingTransport | None = None) -> dict[str, Any]:
    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref", "updates": []}
    body: dict[str, Any] = {"limit": int(limit), "timeout": int(timeout)}
    if offset is not None:
        body["offset"] = int(offset)
    tx = transport or UrllibMessagingTransport()
    status, resp = tx.post(_GET_UPDATES_API.format(token=token), headers={}, body=body, timeout=max(10.0, float(timeout) + 5.0))
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    return {"ok": ok, "status": status, "updates": list(resp.get("result") or []), "response": resp}


def send_chat_action(*, channel_cfg: dict[str, Any] | None = None,
                     chat_id: str | None = None,
                     action: str = "typing",
                     resolve_secret: Callable[[str], str | None] | None = None,
                     transport: MessagingTransport | None = None) -> dict[str, Any]:
    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    effective_chat_id = chat_id or cfg.get("chat_id")
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref"}
    if not effective_chat_id:
        return {"ok": False, "error": "telegram: missing chat_id"}
    tx = transport or UrllibMessagingTransport()
    body = {"chat_id": str(effective_chat_id), "action": action or "typing"}
    status, resp = tx.post(_CHAT_ACTION_API.format(token=token), headers={}, body=body, timeout=10.0)
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    return {"ok": ok, "status": status, "response": resp}


def answer_callback_query(*, channel_cfg: dict[str, Any] | None = None,
                          callback_query_id: str,
                          text: str | None = None,
                          show_alert: bool = False,
                          resolve_secret: Callable[[str], str | None] | None = None,
                          transport: MessagingTransport | None = None) -> dict[str, Any]:
    """Plan 21 P0 §2 — acknowledge an inline-keyboard button press.

    Telegram requires every callback query to be answered within ~15s
    or the spinner stays on the user's screen. We expose a small helper
    that the gateway pipeline uses after dispatching ``approve`` /
    ``reject`` through :class:`ApprovalGate`.
    """
    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref"}
    tx = transport or UrllibMessagingTransport()
    body: dict[str, Any] = {"callback_query_id": str(callback_query_id)}
    if text:
        body["text"] = str(text)
    if show_alert:
        body["show_alert"] = True
    status, resp = tx.post(_ANSWER_CALLBACK_API.format(token=token), headers={}, body=body, timeout=10.0)
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    return {"ok": ok, "status": status, "response": resp}


def clear_reply_markup(*, channel_cfg: dict[str, Any] | None = None,
                       chat_id: str | int,
                       message_id: str | int,
                       resolve_secret: Callable[[str], str | None] | None = None,
                       transport: MessagingTransport | None = None) -> dict[str, Any]:
    """Remove inline buttons from a Telegram message."""
    cfg = channel_cfg or {}
    token = _token(cfg, resolve_secret)
    if not token:
        return {"ok": False, "error": "telegram: missing bot_token_ref"}
    tx = transport or UrllibMessagingTransport()
    status, resp = tx.post(
        _EDIT_REPLY_MARKUP_API.format(token=token),
        headers={},
        body={
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "reply_markup": {"inline_keyboard": []},
        },
        timeout=10.0,
    )
    ok = 200 <= status < 300 and bool(resp.get("ok", True))
    return {"ok": ok, "status": status, "response": resp}
