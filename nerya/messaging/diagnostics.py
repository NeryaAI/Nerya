"""Shared gateway diagnostics for API routes and native tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..core import yaml_io
from ..core.paths import WorkspacePaths
from ..security.secrets import SecretVault
from . import telegram
from .transport import MessagingTransport


def _channel_doc(paths: WorkspacePaths) -> dict[str, Any]:
    doc = yaml_io.load(paths.messages_channels, default={}) or {}
    return doc if isinstance(doc, dict) else {}


def _telegram_channel_cfg(paths: WorkspacePaths, channel: str) -> dict[str, Any]:
    doc = _channel_doc(paths)
    channels = doc.get("channels")
    if not isinstance(channels, dict):
        return {}
    cfg = channels.get(channel)
    return dict(cfg) if isinstance(cfg, dict) else {}


def _vault_secret_resolver(paths: WorkspacePaths) -> Callable[[str], str | None]:
    vault: SecretVault | None = None

    def resolve(ref: str) -> str | None:
        nonlocal vault
        text = str(ref or "").strip()
        if not text.startswith("vault://"):
            return None
        name = text[len("vault://") :].strip()
        if not name:
            return None
        try:
            if vault is None:
                vault = SecretVault.open(Path(paths.vault_enc))
            return vault.resolve(name)
        except Exception:
            return None

    return resolve


def diagnose_telegram_gateway(
    paths: WorkspacePaths,
    *,
    channel: str = "telegram",
    chat_id: str | None = None,
    transport: MessagingTransport | None = None,
) -> dict[str, Any]:
    """Return a structured, secret-safe Telegram gateway diagnostic."""

    channel = str(channel or "telegram").strip() or "telegram"
    cfg = _telegram_channel_cfg(paths, channel)
    token_ref = cfg.get("bot_token_ref") or cfg.get("token_ref")
    configured_chat_id = str(cfg.get("chat_id") or "").strip()
    chat_id_ref = cfg.get("chat_id_ref")
    explicit_chat_id = str(chat_id or "").strip()
    out: dict[str, Any] = {
        "ok": True,
        "platform": "telegram",
        "channel": channel,
        "channels_file_exists": Path(paths.messages_channels).exists(),
        "channel_configured": bool(cfg),
        "configured": {
            "bot_token_ref": bool(token_ref),
            "chat_id": explicit_chat_id or configured_chat_id or None,
            "chat_id_ref": bool(chat_id_ref),
        },
    }
    if not token_ref:
        out["ok"] = False
        out["error"] = "telegram: missing bot_token_ref"
        out["hint"] = "Save your bot token under /settings -> Gateway first."
        return out

    resolver = _vault_secret_resolver(paths)
    bot = telegram.get_me(
        channel_cfg=cfg,
        resolve_secret=resolver,
        transport=transport,
    )
    out["bot"] = bot
    if not bot.get("ok"):
        out["ok"] = False
        out["error"] = bot.get("error") or "telegram: getMe failed"
        out["hint"] = (
            "Telegram rejected the bot token. Generate a fresh token via "
            "@BotFather and re-save it under /settings -> Gateway."
        )
        return out

    if explicit_chat_id or configured_chat_id or chat_id_ref:
        chat = telegram.get_chat(
            channel_cfg=cfg,
            chat_id=explicit_chat_id or None,
            resolve_secret=resolver,
            transport=transport,
        )
        out["chat"] = chat
        if not chat.get("ok"):
            out["ok"] = False
            out["error"] = chat.get("error") or "telegram: getChat failed"
            out["hint"] = (
                "The bot cannot see this chat. For a DM, open the bot and send "
                "/start. For a group, add the bot to the group and send any "
                "message there; group chat_ids are usually negative."
            )
            return out

    return out


__all__ = ["diagnose_telegram_gateway"]
