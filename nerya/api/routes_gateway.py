from __future__ import annotations

from collections import deque
from typing import Any
import json
import mimetypes
import os
import re
import sys
import threading
import time

from ..agent.attachments import upload_chat_attachments
from ..agent.kernel import AgentKernel
from ..core import yaml_io
from ..core.atomic_write import atomic_write_text
from ..core.time import now_iso
from ..messaging import generic_platform, telegram
from ..messaging.diagnostics import diagnose_telegram_gateway
from ..messaging.pipeline import MessagePipeline
from ..messaging.platforms import (
    all_safe_field_keys,
    all_secret_field_keys,
    get_platform,
    list_platforms,
    require_platform,
)
from ..messaging.mirror import GatewayMirror
from ..security.secret_buffer import get_default_buffer
from ..security.secret_scanner import scan_and_redact
from ..security.secrets import SecretVault
from .gateway_commands import (
    CommandContext,
    DEFAULT_REGISTRY as GATEWAY_COMMAND_REGISTRY,
    menu_commands as gateway_menu_commands,
    resolve_dashboard_url,
)
from .gateway_events import compact_turn_summary, hook_status_text, turn_events
from .gateway_identity import (
    first_present,
    message_id as gateway_message_id,
    session_id as gateway_session_id,
)
from .routes_agent import agent_reply_text


_CHANNEL_NAME_OK = re.compile(r"^[a-z][a-z0-9_\-.]{0,80}$")

# Map of every plaintext field name (e.g. ``bot_token``, ``app_secret``,
# ``signing_secret``, ``webhook_url``, ``incoming_webhook_url``,
# ``bridge_url``) to its vault-pointer counterpart. Built from the platform
# catalog so adding a new platform spec automatically extends the schema —
# the previous hard-coded map is preserved as a fallback for legacy
# aliases (``status_url`` etc.).
_SECRET_VALUE_TO_REF = {
    "bot_token": "bot_token_ref",
    "token": "token_ref",
    "webhook_url": "webhook_url_ref",
    "url": "url_ref",
    "incoming_webhook_url": "incoming_webhook_url_ref",
    "status_webhook_url": "status_webhook_url_ref",
    "status_url": "status_webhook_url_ref",
    "auth_header": "auth_header_ref",
}
_SECRET_VALUE_TO_REF.update(all_secret_field_keys())

_SAFE_CONFIG_KEYS = all_safe_field_keys() | {
    "enabled",
    "disabled",
    "chat_id",
    "mode",
    "polling",
    "trade_notifications",
    "approvals",
    "topics",
    "parse_mode",
    "markdown",
    "disable_web_page_preview",
    "username",
    "avatar_url",
    "timeout_s",
    "label",
    "description",
    "auto_reply",
    "allow_unknown_users",
    "allowed_chat_ids",
    "allowed_user_ids",
    "allowed_users",
    "denied_user_ids",
    "group_sessions_per_user",
    "thread_sessions_per_user",
}
_BOOL_CONFIG_KEYS = {
    "enabled",
    "disabled",
    "polling",
    "trade_notifications",
    "approvals",
    "markdown",
    "disable_web_page_preview",
    "auto_reply",
    "allow_unknown_users",
    "group_sessions_per_user",
    "thread_sessions_per_user",
}
_LIST_CONFIG_KEYS = {
    "allowed_chat_ids",
    "allowed_user_ids",
    "allowed_users",
    "denied_user_ids",
}


# --------------------------------------------------------- live-events buffer
# Process-local live status surface. Every gateway turn (Telegram polling and
# generic /gateway/inbound paths) appends ``inbound``, ``phase``, ``outbound``,
# and ``error`` events into a thread-safe ring buffer keyed by a monotonic
# sequence number. The dashboard polls ``GET /gateway/events?since=<seq>``
# every ~1.5s and renders the current per-channel agent activity. The buffer
# is intentionally process-local — a multi-process gateway would back this
# with Redis, but Nerya runs the local API as a single process so the ring
# is the source of truth across all gateway routes.

_GATEWAY_EVENTS_LOCK = threading.Lock()
_GATEWAY_EVENTS: deque[dict[str, Any]] = deque(maxlen=500)
_GATEWAY_EVENTS_SEQ = 0

# Heartbeat throttling — we don't want every poll tick (~1 s) to fan out an
# event. The dashboard only needs to know "polling is alive" once in a while;
# 30 s strikes a balance between freshness and ring-buffer noise.
_GATEWAY_HEARTBEAT_INTERVAL_S = 30.0
_GATEWAY_HEARTBEAT_LAST: dict[str, float] = {}
_GATEWAY_HEARTBEAT_LOCK = threading.Lock()


def _gateway_events_record(event: dict[str, Any]) -> dict[str, Any]:
    """Append a live-status event to the ring buffer.

    Returns the stamped event (with ``seq`` and ``ts_ms``) so callers can
    chain it for tests + outbound webhooks.
    """
    global _GATEWAY_EVENTS_SEQ
    with _GATEWAY_EVENTS_LOCK:
        _GATEWAY_EVENTS_SEQ += 1
        stamped = dict(event)
        stamped.setdefault("ts", now_iso())
        stamped["seq"] = _GATEWAY_EVENTS_SEQ
        stamped["ts_ms"] = int(time.time() * 1000)
        _GATEWAY_EVENTS.append(stamped)
    # Auto-ingest inbound/outbound message events as Evidence Vault rows so
    # the operator has a durable record of every gateway-mediated decision.
    # Heartbeat / phase / error / unknown kinds are skipped to keep the
    # vault signal-to-noise high. Honors ``runtime.evidence_vault`` and
    # never raises.
    try:
        kind = str(stamped.get("kind") or "").lower()
        if kind in ("inbound", "outbound", "message_in", "message_out"):
            direction = "inbound" if kind in ("inbound", "message_in") else "outbound"
            import json as _json
            from ..evidence import autoingest as _evidence_autoingest

            _evidence_autoingest.on_gateway_event(
                None,
                channel=str(
                    stamped.get("channel") or stamped.get("platform") or "unknown"
                ),
                event_id=str(stamped["seq"]),
                direction=direction,
                body=_json.dumps(stamped, default=str, ensure_ascii=False)[:8000],
                operator_id=stamped.get("actor_id") or stamped.get("user_id"),
            )
    except Exception:  # pragma: no cover - defensive
        pass
    return stamped


def _gateway_events_snapshot(*, since: int = 0, channel: str | None = None,
                              platform: str | None = None,
                              limit: int = 100) -> list[dict[str, Any]]:
    with _GATEWAY_EVENTS_LOCK:
        rows = list(_GATEWAY_EVENTS)
    out: list[dict[str, Any]] = []
    chan_filter = (channel or "").strip().lower()
    plat_filter = (platform or "").strip().lower()
    for row in rows:
        if since and int(row.get("seq", 0)) <= since:
            continue
        if chan_filter and str(row.get("channel") or "").lower() != chan_filter:
            continue
        if plat_filter and str(row.get("platform") or "").lower() != plat_filter:
            continue
        out.append(row)
        if len(out) >= max(1, limit):
            break
    return out


def _gateway_event_cursor() -> int:
    with _GATEWAY_EVENTS_LOCK:
        return _GATEWAY_EVENTS_SEQ


def _format_sse(event: dict[str, Any]) -> bytes:
    """Serialise one ring-buffer event as an SSE frame.

    The ``id:`` line lets ``EventSource`` reconnect with the last seen
    sequence number via the Last-Event-ID header on retry, and ``event:``
    lets the client subscribe per-kind (``inbound`` / ``outbound`` /
    ``phase`` / ``error``) when it wants to handle them differently.
    """
    seq = int(event.get("seq") or 0)
    kind = str(event.get("kind") or "message") or "message"
    try:
        payload = json.dumps(event, default=str)
    except (TypeError, ValueError):
        payload = json.dumps({"seq": seq, "kind": kind, "error": "serialise_failed"})
    parts: list[str] = []
    if seq:
        parts.append(f"id: {seq}")
    parts.append(f"event: {kind}")
    parts.append(f"data: {payload}")
    parts.append("")
    parts.append("")
    return ("\n".join(parts)).encode("utf-8")


def reset_gateway_events_for_tests() -> None:
    """Test helper — clear the in-memory event ring + cursor."""
    global _GATEWAY_EVENTS_SEQ
    with _GATEWAY_EVENTS_LOCK:
        _GATEWAY_EVENTS_SEQ = 0
        _GATEWAY_EVENTS.clear()
    with _GATEWAY_HEARTBEAT_LOCK:
        _GATEWAY_HEARTBEAT_LAST.clear()


def _record_gateway_heartbeat(*, platform: str, channel: str,
                              status: str = "ok",
                              note: str | None = None) -> dict[str, Any] | None:
    """Throttled heartbeat — emits at most once per
    ``_GATEWAY_HEARTBEAT_INTERVAL_S`` per channel so the dashboard can
    visually confirm "the poller is alive" without flooding the ring
    buffer. Returns the stamped event when one was emitted, else None.
    """
    key = f"{platform}:{channel}"
    now = time.time()
    with _GATEWAY_HEARTBEAT_LOCK:
        last = _GATEWAY_HEARTBEAT_LAST.get(key, 0.0)
        if now - last < _GATEWAY_HEARTBEAT_INTERVAL_S:
            return None
        _GATEWAY_HEARTBEAT_LAST[key] = now
    payload = {
        "kind": "heartbeat",
        "platform": platform,
        "channel": channel,
        "status": status,
    }
    if note:
        payload["note"] = note
    return _gateway_events_record(payload)


def _record_gateway_error(*, platform: str, channel: str, reason: str,
                          chat_id: str = "", user_id: str = "",
                          detail: str = "",
                          hint: str = "") -> dict[str, Any]:
    """Helper — fan out an `error` event so the dashboard's Live tab can
    show "your bot dropped this update because <reason>" with operator-
    actionable hint text. Always emits (no throttling) because errors are
    rare and hugely informative."""
    payload: dict[str, Any] = {
        "kind": "error",
        "platform": platform,
        "channel": channel,
        "reason": reason,
    }
    if chat_id:
        payload["chat_id"] = str(chat_id)
    if user_id:
        payload["user_id"] = str(user_id)
    if detail:
        payload["detail"] = detail[:280]
    if hint:
        payload["hint"] = hint
    return _gateway_events_record(payload)


def _telegram_cfg(client, channel: str = "telegram") -> dict[str, Any]:
    doc = yaml_io.load(client.config.paths.messages_channels, default={}) or {}
    cfg = (doc.get("channels") or {}).get(channel) or {}
    if not cfg:
        return {"kind": "telegram"}
    return dict(cfg)


def _secret_resolver(client):
    def resolve(ref: str) -> str | None:
        if not ref or not ref.startswith("vault://"):
            return None
        name = ref[len("vault://"):]
        try:
            vault = SecretVault.open(client.config.paths.vault_enc)
            return vault.resolve(name, required_scope="messaging")
        except Exception:
            return None
    return resolve


# gateway_menu_commands() is provided by gateway_commands; the shared menu
# is the single source of truth for setup, startup sync, and Telegram polling.


def _configured_telegram_channels(client) -> list[tuple[str, dict[str, Any]]]:
    doc = yaml_io.load(client.config.paths.messages_channels, default={}) or {}
    channels = doc.get("channels") or {}
    configured: list[tuple[str, dict[str, Any]]] = []
    for name, raw_cfg in channels.items():
        cfg = dict(raw_cfg or {})
        kind = str(cfg.get("kind") or ("telegram" if name == "telegram" else "")).lower()
        token_ref = cfg.get("bot_token_ref") or cfg.get("token_ref")
        if kind == "telegram" and token_ref:
            configured.append((str(name), cfg))
    return configured


def _configured_gateway_channels(client) -> list[tuple[str, dict[str, Any], str]]:
    doc = yaml_io.load(client.config.paths.messages_channels, default={}) or {}
    channels = doc.get("channels") if isinstance(doc, dict) else {}
    if not isinstance(channels, dict):
        return []
    configured: list[tuple[str, dict[str, Any], str]] = []
    for name, raw_cfg in channels.items():
        cfg = dict(raw_cfg or {})
        kind = _gateway_kind(str(name), cfg)
        if kind in {"dashboard", "local"}:
            continue
        if _gateway_channel_configured(kind, cfg):
            configured.append((str(name), cfg, kind))
    return configured


def _gateway_kind(channel: str, cfg: dict[str, Any]) -> str:
    return str(
        cfg.get("kind") or ("telegram" if channel == "telegram" else channel)
    ).strip().lower()


def _gateway_channel_configured(kind: str, cfg: dict[str, Any]) -> bool:
    """Return True only when every required auth field for ``kind`` is present.

    Driven by ``platforms.GatewayPlatformSpec.secret_fields`` — adding a new
    platform automatically extends the configured-check, and the dashboard
    can use the same spec to render the right form. Any legacy channel that
    only set ``webhook_url``/``url`` still validates via the kind-specific
    fallbacks because we accept either the plaintext key or its ``_ref``
    counterpart.
    """
    if cfg.get("disabled") is True or cfg.get("enabled") is False:
        return False
    spec = get_platform(kind)
    if spec is None:
        # Unknown id — fall back to the loose "any auth field" check so a
        # newly-added platform doesn't disappear from /gateway/status.
        return any(
            cfg.get(key)
            for key in (
                "webhook_url",
                "webhook_url_ref",
                "incoming_webhook_url",
                "incoming_webhook_url_ref",
                "url",
                "url_ref",
                "bot_token_ref",
                "token_ref",
            )
        )
    required_keys = [(f.key, f.ref_key) for f in spec.secret_fields if f.required]
    if not required_keys:
        # Pure send_only/inbound stub with no required auth (e.g. local).
        # Treat as configured when the channel exists and is enabled.
        return True
    for plain_key, ref_key in required_keys:
        if not (cfg.get(plain_key) or cfg.get(ref_key)):
            # Telegram historically also accepts the legacy ``token_ref``
            # alias for ``bot_token_ref``; preserve that.
            if plain_key == "bot_token" and cfg.get("token_ref"):
                continue
            return False
    return True


def _gateway_runtime_mode(kind: str, cfg: dict[str, Any]) -> str:
    if kind == "telegram":
        return str(cfg.get("mode") or "polling")
    if cfg.get("mode"):
        return str(cfg.get("mode"))
    return "send_only"


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.replace(",", " ").split() if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _identity_from_payload(payload: dict[str, Any], *, platform: str) -> dict[str, str]:
    chat_id = first_present([
        payload.get("chat_id"),
        payload.get("conversation_id"),
        payload.get("conversationId"),
        payload.get("channel_id"),
        payload.get("room_id"),
        payload.get("roomId"),
        payload.get("group_id"),
        payload.get("FromUserName"),
        payload.get("thread_id") if not payload.get("chat_id") else None,
    ])
    user_id = first_present([
        payload.get("user_id"),
        payload.get("actor_id"),
        payload.get("sender_id"),
        payload.get("senderStaffId"),
        payload.get("from_id"),
        payload.get("author_id"),
        payload.get("FromUserName"),
    ])
    actor_id = first_present([
        payload.get("actor_id"),
        payload.get("username"),
        payload.get("user_name"),
        payload.get("sender_name"),
        user_id,
    ])
    thread_id = first_present([
        payload.get("thread_id"),
        payload.get("message_thread_id"),
        payload.get("topic_id"),
    ])
    chat_type = str(payload.get("chat_type") or payload.get("conversation_type") or "").strip().lower()
    if not chat_type:
        chat_type = "group" if user_id and chat_id and user_id != chat_id else "dm"
    return {
        "platform": platform,
        "chat_id": chat_id or "default",
        "user_id": user_id,
        "actor_id": actor_id or user_id or chat_id or platform,
        "thread_id": thread_id,
        "chat_type": chat_type,
    }


def _dict_at(value: Any, *path: str) -> dict[str, Any]:
    cur = value
    for key in path:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(key)
    return cur if isinstance(cur, dict) else {}


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalise_command_text(command: Any, text: Any = "") -> str:
    command_text = str(command or "").strip()
    tail = str(text or "").strip()
    if not command_text:
        return tail
    if not command_text.startswith("/"):
        command_text = "/" + command_text
    return f"{command_text} {tail}".strip()


def _discord_slash_text(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    name = str(data.get("name") or "").strip()
    if not name:
        return ""
    args: list[str] = []
    for opt in data.get("options") or []:
        if not isinstance(opt, dict):
            continue
        value = opt.get("value")
        if value is None:
            continue
        args.append(str(value).strip())
    return _normalise_command_text(name, " ".join(arg for arg in args if arg))


def _identity_payload_from_inbound(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    d = payload.get("d") if isinstance(payload.get("d"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    author = _first_dict(
        payload.get("author"),
        payload.get("user"),
        payload.get("sender"),
        _dict_at(payload, "member", "user"),
        _dict_at(payload, "event", "user"),
        _dict_at(payload, "event", "sender"),
        _dict_at(payload, "d", "author"),
        _dict_at(payload, "d", "member", "user"),
        _dict_at(payload, "message", "author"),
        _dict_at(payload, "message", "from"),
        _dict_at(payload, "message", "sender"),
        _dict_at(payload, "sender", "sender_id"),
        _dict_at(payload, "event", "sender", "sender_id"),
    )
    msg_obj = _first_dict(
        payload.get("message"),
        payload.get("edited_message"),
        event.get("message"),
        d,
    )
    message_from_list = {}
    messages = payload.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        message_from_list = messages[0]
    contacts = payload.get("contacts")
    contact = contacts[0] if isinstance(contacts, list) and contacts and isinstance(contacts[0], dict) else {}
    chat = _first_dict(
        payload.get("chat"),
        msg_obj.get("chat") if isinstance(msg_obj, dict) else {},
        event.get("chat") if isinstance(event, dict) else {},
    )
    bridge_payload = any(
        payload.get(key)
        for key in (
            "conversationId",
            "senderStaffId",
            "FromUserName",
            "ToUserName",
            "roomId",
            "room_id",
        )
    )
    if event or d or data or author or message_from_list or bridge_payload:
        return {
            **payload,
            "chat_id": first_present([
                payload.get("chat_id"),
                payload.get("conversation_id"),
                payload.get("conversationId"),
                payload.get("channel_id"),
                payload.get("room_id"),
                payload.get("group_id"),
                payload.get("roomId"),
                event.get("channel"),
                event.get("channel_id"),
                event.get("chat_id"),
                event.get("open_chat_id"),
                d.get("channel_id"),
                d.get("guild_id"),
                data.get("channel_id"),
                msg_obj.get("chat_id") if isinstance(msg_obj, dict) else None,
                msg_obj.get("room_id") if isinstance(msg_obj, dict) else None,
                chat.get("id"),
                message_from_list.get("from"),
                _dict_at(payload, "metadata").get("phone_number_id"),
                payload.get("FromUserName"),
            ]),
            "user_id": first_present([
                payload.get("user_id"),
                payload.get("user"),
                payload.get("sender_id"),
                payload.get("senderStaffId"),
                payload.get("FromUserId"),
                payload.get("FromUserName"),
                event.get("user"),
                event.get("sender_id"),
                event.get("senderStaffId"),
                d.get("user_id"),
                author.get("id"),
                author.get("user_id"),
                author.get("open_id"),
                author.get("union_id"),
                author.get("sender_id"),
                message_from_list.get("from"),
                contact.get("wa_id"),
            ]),
            "actor_id": first_present([
                payload.get("actor_id"),
                payload.get("user_name"),
                payload.get("username"),
                payload.get("senderNick"),
                event.get("username"),
                event.get("user_name"),
                author.get("username"),
                author.get("name"),
                author.get("nickname"),
                author.get("id"),
                contact.get("profile", {}).get("name") if isinstance(contact.get("profile"), dict) else None,
            ]),
            "thread_id": first_present([
                payload.get("thread_id"),
                payload.get("thread_ts"),
                payload.get("root_id"),
                payload.get("event_id"),
                event.get("thread_ts"),
                event.get("event_id"),
                event.get("message_id"),
                d.get("id"),
                d.get("message_id"),
                data.get("id"),
                msg_obj.get("message_thread_id") if isinstance(msg_obj, dict) else None,
                msg_obj.get("thread_id") if isinstance(msg_obj, dict) else None,
                msg_obj.get("root_id") if isinstance(msg_obj, dict) else None,
                message_from_list.get("id"),
                payload.get("MsgId"),
            ]),
            "chat_type": first_present([
                payload.get("chat_type"),
                payload.get("conversation_type"),
                payload.get("channel_type"),
                event.get("channel_type"),
                event.get("chat_type"),
                chat.get("type"),
                "group" if payload.get("guild_id") or d.get("guild_id") else None,
            ]),
        }
    cb = payload.get("callback_query") if isinstance(payload.get("callback_query"), dict) else {}
    if cb:
        actor = cb.get("from") or {}
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        return {
            **payload,
            "chat_id": first_present([payload.get("chat_id"), chat.get("id")]),
            "user_id": first_present([payload.get("user_id"), actor.get("id")]),
            "actor_id": first_present([
                payload.get("actor_id"),
                actor.get("username"),
                actor.get("id"),
            ]),
            "thread_id": first_present([
                payload.get("thread_id"),
                msg.get("message_thread_id"),
            ]),
            "chat_type": first_present([payload.get("chat_type"), chat.get("type")]),
        }
    msg = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    if not msg:
        msg = payload.get("edited_message") if isinstance(payload.get("edited_message"), dict) else {}
    if not msg:
        return payload
    chat = msg.get("chat") or {}
    actor = msg.get("from") or msg.get("author") or {}
    return {
        **payload,
        "chat_id": first_present([
            payload.get("chat_id"),
            chat.get("id"),
            msg.get("chat_id"),
            msg.get("channel_id"),
        ]),
        "user_id": first_present([
            payload.get("user_id"),
            actor.get("id"),
            msg.get("user_id"),
            msg.get("author_id"),
        ]),
        "actor_id": first_present([
            payload.get("actor_id"),
            actor.get("username"),
            actor.get("name"),
            actor.get("id"),
            msg.get("actor_id"),
        ]),
        "thread_id": first_present([
            payload.get("thread_id"),
            msg.get("message_thread_id"),
            msg.get("thread_id"),
        ]),
        "chat_type": first_present([payload.get("chat_type"), chat.get("type")]),
    }


def _text_from_inbound_payload(payload: dict[str, Any]) -> str:
    direct_text = payload.get("text")
    if isinstance(direct_text, dict):
        text = first_present([
            direct_text.get("content"),
            direct_text.get("body"),
            direct_text.get("text"),
        ])
        if text:
            return text
    direct_content = payload.get("content")
    if isinstance(direct_content, dict):
        text = first_present([
            direct_content.get("text"),
            direct_content.get("body"),
            direct_content.get("content"),
        ])
        if text:
            return text
    text = first_present([
        direct_text if not isinstance(direct_text, dict) else None,
        direct_content if not isinstance(direct_content, dict) else None,
        payload.get("body"),
        payload.get("Content"),
    ])
    if text:
        return text
    slash = _discord_slash_text(payload)
    if slash:
        return slash
    if payload.get("command"):
        return _normalise_command_text(payload.get("command"), payload.get("text"))
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    if event:
        event_text_obj = event.get("text")
        if isinstance(event_text_obj, dict):
            text = first_present([
                event_text_obj.get("content"),
                event_text_obj.get("body"),
                event_text_obj.get("text"),
            ])
            if text:
                return text
        event_text = first_present([
            event_text_obj if not isinstance(event_text_obj, dict) else None,
            event.get("content"),
            event.get("body"),
        ])
        if event_text:
            return event_text
        event_msg = event.get("message") if isinstance(event.get("message"), dict) else {}
        content_obj = _json_object(event_msg.get("content"))
        event_msg_text = first_present([
            event_msg.get("text"),
            event_msg.get("content") if not content_obj else None,
            event_msg.get("body"),
            content_obj.get("text"),
            _dict_at(event_msg, "text").get("content"),
            _dict_at(event_msg, "text").get("body"),
        ])
        if event_msg_text:
            return event_msg_text
    d = payload.get("d") if isinstance(payload.get("d"), dict) else {}
    d_text = first_present([d.get("content"), d.get("text"), d.get("body")])
    if d_text:
        return d_text
    msg = payload.get("message")
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        content_obj = _json_object(msg.get("content"))
        nested_text = first_present([
            msg.get("text"),
            msg.get("content") if not content_obj else None,
            msg.get("message"),
            msg.get("body"),
            content_obj.get("text"),
            _dict_at(msg, "text").get("content"),
            _dict_at(msg, "text").get("body"),
            _dict_at(msg, "content").get("text"),
            _dict_at(msg, "content").get("body"),
        ])
        if nested_text:
            return nested_text
    edited = payload.get("edited_message")
    if isinstance(edited, dict):
        return first_present([edited.get("text"), edited.get("content")])
    messages = payload.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        item = messages[0]
        return first_present([
            _dict_at(item, "text").get("body"),
            _dict_at(item, "interactive", "button_reply").get("id"),
            _dict_at(item, "interactive", "list_reply").get("id"),
            item.get("body"),
            item.get("text"),
        ])
    return ""


def _attachments_from_inbound_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise bridge/dashboard attachment envelopes for agent turns."""

    out: list[dict[str, Any]] = []
    for container in _inbound_attachment_containers(payload):
        out.extend(_attachments_from_container(container))
    return _dedupe_inbound_attachments([item for item in out if item])


def _inbound_attachment_containers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = [payload]
    for value in (
        payload.get("message"),
        payload.get("edited_message"),
        payload.get("event"),
        _dict_at(payload, "event", "message"),
        payload.get("d"),
        _dict_at(payload, "d", "message"),
        payload.get("data"),
    ):
        if isinstance(value, dict):
            containers.append(value)
            content_obj = _json_object(value.get("content"))
            if content_obj:
                containers.append(content_obj)
    messages = payload.get("messages")
    if isinstance(messages, list):
        containers.extend(item for item in messages if isinstance(item, dict))
    return containers


def _attachments_from_container(container: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in ("attachments", "files", "images", "documents", "media"):
        value = container.get(key)
        out.extend(_attachments_from_value(value, kind_hint=_kind_hint_from_key(key)))
    for key, kind_hint in (
        ("document", "document"),
        ("file", "file"),
        ("image", "image"),
        ("photo", "image"),
        ("photos", "image"),
        ("video", "video"),
        ("videos", "video"),
        ("animation", "video"),
        ("audio", "audio"),
        ("audios", "audio"),
        ("voice", "audio"),
        ("sticker", "image"),
    ):
        out.extend(_attachments_from_value(container.get(key), kind_hint=kind_hint))
    return out


def _attachments_from_value(value: Any, *, kind_hint: str = "") -> list[dict[str, Any]]:
    if isinstance(value, list):
        if kind_hint == "image" and value and all(isinstance(item, dict) and item.get("file_id") for item in value):
            # Telegram sends photo sizes as an array; use the largest variant.
            item = value[-1] if isinstance(value[-1], dict) else {}
            return [_normalise_inbound_attachment(item, kind_hint=kind_hint)] if item else []
        return [
            _normalise_inbound_attachment(item, kind_hint=kind_hint)
            for item in value
            if isinstance(item, dict)
        ]
    if isinstance(value, dict):
        return [_normalise_inbound_attachment(value, kind_hint=kind_hint)]
    return []


def _normalise_inbound_attachment(value: Any, *, kind_hint: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    name = str(
        value.get("name")
        or value.get("filename")
        or value.get("file_name")
        or value.get("title")
        or value.get("file_id")
        or value.get("id")
        or "attachment"
    )
    mime = str(
        value.get("mime_type")
        or value.get("media_type")
        or value.get("content_type")
        or value.get("contentType")
        or value.get("mimetype")
        or value.get("mimeType")
        or value.get("filetype")
        )
    if not mime or "/" not in mime:
        guessed, _ = mimetypes.guess_type(name)
        mime = guessed or _mime_from_kind(kind_hint or str(value.get("kind") or value.get("type") or ""))
    kind = _attachment_kind(kind_hint or str(value.get("kind") or value.get("type") or value.get("msgtype") or ""), mime)
    return {
        key: val
        for key, val in {
            "id": value.get("id") or value.get("file_id"),
            "name": name,
            "mime_type": mime,
            "kind": kind,
            "size": value.get("size") or value.get("file_size"),
            "data_url": value.get("data_url") or value.get("data_uri"),
            "data": value.get("data") or value.get("base64") or value.get("content_b64") or value.get("bytes_b64"),
            "url": (
                value.get("url")
                or value.get("download_url")
                or value.get("file_url")
                or value.get("proxy_url")
                or value.get("url_private")
                or value.get("permalink")
                or value.get("media_url")
                or value.get("mediaUrl")
            ),
            "artifact_uri": value.get("artifact_uri"),
            "text": value.get("text") if isinstance(value.get("text"), str) else value.get("caption"),
            "file_id": value.get("file_id"),
            "file_unique_id": value.get("file_unique_id"),
        }.items()
        if val not in (None, "")
    }


def _kind_hint_from_key(key: str) -> str:
    if key in {"images"}:
        return "image"
    if key in {"documents"}:
        return "document"
    if key in {"files"}:
        return "file"
    return ""


def _mime_from_kind(kind: str) -> str:
    kind = kind.lower()
    if kind == "image":
        return "image/jpeg"
    if kind == "video":
        return "video/mp4"
    if kind == "audio":
        return "audio/mpeg"
    return "application/octet-stream"


def _attachment_kind(kind: str, mime: str) -> str:
    normalized = (kind or "").lower()
    if normalized in {"image", "video", "audio"}:
        return normalized
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime == "application/pdf" or mime.startswith("text/"):
        return "document"
    if normalized in {"document", "file"}:
        return normalized
    return "file"


def _dedupe_inbound_attachments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = str(
            item.get("id")
            or item.get("file_id")
            or item.get("artifact_uri")
            or item.get("url")
            or f"{item.get('name')}:{item.get('size')}"
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _prepare_inbound_attachments(
    client,
    payload: dict[str, Any],
    *,
    platform: str,
    cfg: dict[str, Any],
    upload_id: str = "",
) -> list[dict[str, Any]]:
    attachments = _attachments_from_inbound_payload(payload)
    if not attachments:
        return []
    if platform == "telegram":
        attachments = _download_telegram_inbound_attachments(client, cfg, attachments)
    return _persist_inline_inbound_attachments(
        client,
        attachments,
        upload_id=upload_id or _inbound_upload_id(platform, payload),
    )


def _download_telegram_inbound_attachments(
    client,
    cfg: dict[str, Any],
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    resolver = _secret_resolver(client)
    for item in attachments:
        if not isinstance(item, dict):
            continue
        if item.get("data") or item.get("data_url") or item.get("artifact_uri") or item.get("url"):
            out.append(item)
            continue
        file_id = str(item.get("file_id") or item.get("id") or "").strip()
        if not file_id:
            out.append(item)
            continue
        downloaded = telegram.download_inbound_file(
            channel_cfg=cfg,
            file_id=file_id,
            resolve_secret=resolver,
        )
        if downloaded.get("ok"):
            merged = dict(item)
            merged["data"] = downloaded.get("data")
            merged["size"] = item.get("size") or downloaded.get("file_size")
            if downloaded.get("content_type") and not merged.get("mime_type"):
                merged["mime_type"] = downloaded.get("content_type")
            merged["telegram_file_path"] = downloaded.get("file_path")
            out.append(merged)
        else:
            failed = dict(item)
            failed["reason"] = str(downloaded.get("error") or "telegram_download_failed")
            out.append(failed)
    return out


def _persist_inline_inbound_attachments(
    client,
    attachments: list[dict[str, Any]],
    *,
    upload_id: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, item in enumerate(attachments):
        if not isinstance(item, dict):
            continue
        if item.get("artifact_uri") or item.get("url"):
            out.append(item)
            continue
        if not _has_inline_attachment_content(item):
            out.append(item)
            continue
        uploaded = upload_chat_attachments(
            [item],
            paths=client.config.paths,
            upload_id=f"{upload_id}_{index}",
        )
        out.append(uploaded[0] if uploaded else item)
    return out


def _has_inline_attachment_content(item: dict[str, Any]) -> bool:
    return any(
        item.get(key)
        for key in ("data_url", "data", "base64", "content_b64", "bytes_b64", "text")
    )


def _inbound_upload_id(platform: str, payload: dict[str, Any]) -> str:
    msg = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    messages = payload.get("messages")
    first_message = messages[0] if isinstance(messages, list) and messages and isinstance(messages[0], dict) else {}
    value = first_present([
        payload.get("upload_id"),
        payload.get("message_id"),
        payload.get("update_id"),
        msg.get("message_id"),
        msg.get("id"),
        event.get("message_id"),
        event.get("event_id"),
        first_message.get("id"),
    ])
    return f"{platform}_{value or int(time.time() * 1000)}"


def _session_key_for_identity(platform: str, identity: dict[str, str],
                              cfg: dict[str, Any]) -> str:
    chat_id = identity.get("chat_id") or "default"
    user_id = identity.get("user_id") or ""
    thread_id = identity.get("thread_id") or ""
    chat_type = (identity.get("chat_type") or "").lower()
    key_parts = [platform, chat_id]
    if thread_id:
        key_parts.append(f"thread:{thread_id}")
    group_per_user = cfg.get("group_sessions_per_user", True) is not False
    thread_per_user = cfg.get("thread_sessions_per_user", False) is True
    isolate_user = bool(user_id) and (
        chat_type == "dm"
        or (thread_id and thread_per_user)
        or (not thread_id and chat_type in {"group", "channel", "room"} and group_per_user)
    )
    if isolate_user:
        key_parts.append(f"user:{user_id}")
    return ":".join(str(part) for part in key_parts if str(part))


def _session_id_for_identity(platform: str, identity: dict[str, str],
                             cfg: dict[str, Any],
                             active_sessions: dict[str, Any],
                             explicit_session_id: Any = None) -> tuple[str, str]:
    session_key = _session_key_for_identity(platform, identity, cfg)
    chat_id = identity.get("chat_id") or "default"
    user_id = identity.get("user_id") or ""
    thread_id = identity.get("thread_id") or ""
    session = (
        explicit_session_id
        or active_sessions.get(session_key)
        or active_sessions.get(f"{platform}:{chat_id}")
        or active_sessions.get(str(chat_id))
        or gateway_session_id(
            platform,
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id if session_key.endswith(f"user:{user_id}") else None,
        )
    )
    return session_key, str(session)


def _gateway_actor_allowed(cfg: dict[str, Any],
                           identity: dict[str, str]) -> tuple[bool, str]:
    chat_id = identity.get("chat_id") or ""
    user_id = identity.get("user_id") or ""
    actor_id = identity.get("actor_id") or ""
    allowed_chats = set(_as_list(cfg.get("allowed_chat_ids")))
    allowed_users = set(_as_list(cfg.get("allowed_user_ids") or cfg.get("allowed_users")))
    denied_users = set(_as_list(cfg.get("denied_user_ids")))
    user_candidates = {v for v in (user_id, actor_id) if v}

    if denied_users and user_candidates.intersection(denied_users):
        return False, "user_denied"
    if allowed_chats and chat_id not in allowed_chats:
        return False, "chat_not_allowed"
    if allowed_users and not user_candidates.intersection(allowed_users):
        return False, "user_not_allowed"
    if not allowed_users and not allowed_chats and cfg.get("allow_unknown_users") is False:
        return False, "unknown_users_disabled"
    return True, ""


def _callback_data_from_payload(payload: dict[str, Any]) -> str:
    direct = first_present([
        payload.get("callback_data") if isinstance(payload.get("callback_data"), str) else None,
        payload.get("approval_callback") if isinstance(payload.get("approval_callback"), str) else None,
        payload.get("data") if isinstance(payload.get("data"), str) else None,
    ])
    if direct:
        return direct
    callback = payload.get("callback") if isinstance(payload.get("callback"), dict) else {}
    direct = first_present([
        callback.get("callback_data"),
        callback.get("approval_callback"),
        callback.get("data"),
    ])
    if direct:
        return direct
    cb = payload.get("callback_query") if isinstance(payload.get("callback_query"), dict) else {}
    if cb.get("data"):
        return first_present([cb.get("data")])
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
    action_value = action.get("value") if isinstance(action.get("value"), dict) else {}
    actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
    first_action = actions[0] if actions and isinstance(actions[0], dict) else {}
    return first_present([
        data.get("custom_id"),
        data.get("callback_data"),
        action.get("callback_data"),
        action.get("value") if isinstance(action.get("value"), str) else None,
        action_value.get("callback_data"),
        action_value.get("value"),
        first_action.get("value"),
        first_action.get("action_id"),
        _dict_at(first_action, "selected_option").get("value"),
    ])


def _handle_gateway_approval_callback(client, *,
                                      platform: str,
                                      channel: str,
                                      identity: dict[str, str],
                                      callback_data: str) -> dict[str, Any]:
    from . import routes_approvals as _ra

    result = _ra._callback(
        client,
        {
            "callback_data": callback_data,
            "actor_id": identity.get("actor_id") or identity.get("user_id") or platform,
        },
    )
    result = dict(result or {})
    result.setdefault("kind", "approval_callback")
    result["platform"] = platform
    result["channel"] = channel
    result["chat_id"] = identity.get("chat_id") or ""
    result["user_id"] = identity.get("user_id") or ""
    result["actor_id"] = identity.get("actor_id") or ""
    return result


def _trim_command_description(value: Any, *, limit: int = 90) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _gateway_command_catalog(platform: str | None = None) -> dict[str, Any]:
    commands = gateway_menu_commands(platform=platform)
    text_commands = [
        {
            "text": "/" + str(row.get("command") or "").strip().lstrip("/"),
            "description": row.get("description") or "",
        }
        for row in commands
    ]
    slash_commands = [
        {
            "name": str(row.get("command") or "").strip().lstrip("/"),
            "description": _trim_command_description(row.get("description")),
            "type": 1,
        }
        for row in commands
    ]
    keyword_commands = [
        {
            "keyword": "/" + str(row.get("command") or "").strip().lstrip("/"),
            "description": row.get("description") or "",
        }
        for row in commands
    ]
    return {
        "commands": commands,
        "text_commands": text_commands,
        "slash_commands": slash_commands,
        "telegram": {"set_my_commands": commands},
        "discord": {"application_commands": slash_commands},
        "slack": {"slash_commands": slash_commands, "event_text_commands": text_commands},
        "mattermost": {"slash_commands": slash_commands, "text_commands": text_commands},
        "feishu": {"bot_menu": keyword_commands, "event_text_commands": text_commands},
        "wecom": {"menu_keywords": keyword_commands, "event_text_commands": text_commands},
        "dingtalk": {"robot_keywords": keyword_commands, "event_text_commands": text_commands},
        "matrix": {"text_commands": text_commands},
        "whatsapp": {"text_commands": text_commands},
        "signal": {"text_commands": text_commands},
        "sms": {"text_commands": text_commands},
        "email": {"text_commands": text_commands},
        "generic": {"text_commands": text_commands},
    }


def _channels_doc(client) -> dict[str, Any]:
    doc = yaml_io.load(client.config.paths.messages_channels, default={}) or {}
    return doc if isinstance(doc, dict) else {}


def _write_channels_doc(client, doc: dict[str, Any]) -> None:
    if not isinstance(doc.get("channels"), dict):
        doc["channels"] = {}
    yaml_io.dump(client.config.paths.messages_channels, doc)


def _public_secret_ref(ref: Any) -> dict[str, Any]:
    if isinstance(ref, str) and ref.startswith("vault://"):
        return {"configured": True, "ref": ref}
    return {"configured": bool(ref), "ref": None}


def _safe_gateway_delivery(result: dict[str, Any]) -> dict[str, Any]:
    blocked = {
        "platform_response",
        "response",
        "webhook_url",
        "url",
        "incoming_webhook_url",
        "bot_token",
        "token",
        "auth_header",
    }
    safe: dict[str, Any] = {}
    for key, value in result.items():
        if key in blocked or key.endswith("_secret") or key.endswith("_token"):
            continue
        safe[key] = value
    return safe


def _public_gateway_channel(channel: str, cfg: dict[str, Any]) -> dict[str, Any]:
    kind = _gateway_kind(channel, cfg)
    spec = get_platform(kind)
    safe_cfg = {
        key: cfg.get(key)
        for key in sorted(_SAFE_CONFIG_KEYS)
        if key in cfg
    }
    secret_refs: dict[str, dict[str, Any]] = {}
    for secret_key, ref_key in _SECRET_VALUE_TO_REF.items():
        ref = cfg.get(ref_key)
        legacy_plaintext = bool(cfg.get(secret_key))
        if ref or legacy_plaintext:
            item = _public_secret_ref(ref)
            if legacy_plaintext and not ref:
                item["legacy_plaintext"] = True
            secret_refs[ref_key] = item
    return {
        "channel": channel,
        "kind": kind,
        "platform": kind,
        "title": spec.title if spec else kind,
        "support_level": spec.support_level if spec else "unknown",
        "configured": _gateway_channel_configured(kind, cfg),
        "enabled": cfg.get("enabled") is not False and cfg.get("disabled") is not True,
        "mode": _gateway_runtime_mode(kind, cfg),
        "outbound_ready": _gateway_channel_configured(kind, cfg),
        "config": safe_cfg,
        "secret_refs": secret_refs,
    }


def gateway_config_snapshot(client) -> dict[str, Any]:
    """Return editable gateway config without plaintext secret values."""
    doc = _channels_doc(client)
    channels = doc.get("channels") if isinstance(doc.get("channels"), dict) else {}
    public_channels = [
        _public_gateway_channel(str(name), dict(raw_cfg or {}))
        for name, raw_cfg in sorted(channels.items())
        if _gateway_kind(str(name), dict(raw_cfg or {})) not in {"dashboard", "local"}
    ]
    return {
        "ok": True,
        "channels_file_exists": client.config.paths.messages_channels.exists(),
        "channels": public_channels,
        "platforms": list_platforms(),
        "status": gateway_runtime_status(client),
    }


def _clean_gateway_channel_name(raw: Any) -> str:
    channel = str(raw or "").strip().lower()
    if not channel or not _CHANNEL_NAME_OK.match(channel):
        raise ValueError("invalid_channel")
    return channel


def _secret_name(channel: str, key: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_\-.]+", "_", f"gateway_{channel}_{key}".lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_.-")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"gateway_{cleaned}"
    return cleaned[:80]


def _store_gateway_secret(client, *, channel: str, key: str, value: str) -> str:
    vault = SecretVault.open(client.config.paths.vault_enc)
    kind = (
        "token" if "token" in key or "auth" in key
        else "webhook_url" if "url" in key
        else "opaque"
    )
    meta = vault.put(
        name=_secret_name(channel, key),
        value=value,
        kind=kind,
        scope=["messaging"],
        owner="dashboard",
    )
    return meta.ref()


def _coerce_topics(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _apply_public_gateway_fields(cfg: dict[str, Any], payload: dict[str, Any]) -> None:
    for key in _SAFE_CONFIG_KEYS:
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None or value == "":
            cfg.pop(key, None)
            continue
        if key in _BOOL_CONFIG_KEYS:
            cfg[key] = bool(value)
        elif key in _LIST_CONFIG_KEYS:
            cfg[key] = _as_list(value)
        elif key == "topics":
            topics = _coerce_topics(value)
            if topics:
                cfg[key] = topics
            else:
                cfg.pop(key, None)
        elif key == "timeout_s":
            try:
                timeout = float(value)
            except (TypeError, ValueError):
                continue
            if timeout > 0:
                cfg[key] = timeout
        else:
            cfg[key] = str(value).strip()


def _apply_secret_gateway_fields(client, *, channel: str,
                                 cfg: dict[str, Any],
                                 payload: dict[str, Any]) -> None:
    for secret_key, ref_key in _SECRET_VALUE_TO_REF.items():
        raw_ref = payload.get(ref_key)
        raw_value = payload.get(secret_key)
        if isinstance(raw_ref, str) and raw_ref.strip():
            value = raw_ref.strip()
            cfg[ref_key] = value if value.startswith("vault://") else _store_gateway_secret(
                client, channel=channel, key=secret_key, value=value,
            )
            cfg.pop(secret_key, None)
            continue
        if isinstance(raw_value, str) and raw_value.strip():
            value = raw_value.strip()
            cfg[ref_key] = value if value.startswith("vault://") else _store_gateway_secret(
                client, channel=channel, key=secret_key, value=value,
            )
            cfg.pop(secret_key, None)

    # Repair legacy plaintext configs opportunistically when the operator
    # touches a gateway channel from the Settings page.
    for secret_key, ref_key in _SECRET_VALUE_TO_REF.items():
        value = cfg.get(secret_key)
        if isinstance(value, str) and value.strip() and not cfg.get(ref_key):
            cfg[ref_key] = value if value.startswith("vault://") else _store_gateway_secret(
                client, channel=channel, key=secret_key, value=value.strip(),
            )
        cfg.pop(secret_key, None)


def gateway_config_upsert(client, payload: dict[str, Any]) -> dict[str, Any]:
    channel = _clean_gateway_channel_name(payload.get("channel"))
    platform = str(payload.get("platform") or payload.get("kind") or channel).strip().lower()
    spec = require_platform(platform)
    doc = _channels_doc(client)
    channels = doc.get("channels") if isinstance(doc.get("channels"), dict) else {}
    cfg = dict(channels.get(channel) or {})
    cfg["kind"] = spec.id
    _apply_public_gateway_fields(cfg, payload)
    _apply_secret_gateway_fields(client, channel=channel, cfg=cfg, payload=payload)
    channels[channel] = cfg
    doc["channels"] = channels
    _write_channels_doc(client, doc)
    startup: dict[str, Any] | None = None
    try:
        startup = launch_configured_gateways_on_start(client)
    except Exception as exc:
        startup = {"scheduled": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "channel": _public_gateway_channel(channel, cfg),
        "startup": startup,
        "config": gateway_config_snapshot(client),
    }


def gateway_config_delete(client, payload: dict[str, Any]) -> dict[str, Any]:
    channel = _clean_gateway_channel_name(payload.get("channel"))
    doc = _channels_doc(client)
    channels = doc.get("channels") if isinstance(doc.get("channels"), dict) else {}
    existed = channel in channels
    channels.pop(channel, None)
    doc["channels"] = channels
    _write_channels_doc(client, doc)
    return {"ok": True, "channel": channel, "deleted": existed,
            "config": gateway_config_snapshot(client)}


def gateway_config_test(client, payload: dict[str, Any]) -> dict[str, Any]:
    channel = _clean_gateway_channel_name(payload.get("channel"))
    text = str(payload.get("text") or "").strip() or "Nerya gateway test message."
    doc = _channels_doc(client)
    channels = doc.get("channels") if isinstance(doc.get("channels"), dict) else {}
    if channel not in channels:
        return {"ok": False, "error": "channel_not_configured", "channel": channel}
    cfg = dict(channels.get(channel) or {})
    platform = _gateway_kind(channel, cfg)
    mode = str(payload.get("mode") or "").strip().lower()
    send_only = mode in {"send", "send_only", "outbound"} or payload.get("send_only") is True

    if not send_only:
        allowed_chats = _as_list(cfg.get("allowed_chat_ids"))
        allowed_users = _as_list(cfg.get("allowed_user_ids") or cfg.get("allowed_users"))
        chat_id = str(
            payload.get("chat_id")
            or cfg.get("chat_id")
            or (allowed_chats[0] if allowed_chats else "")
            or f"gateway-test-{channel}"
        )
        user_id = str(
            payload.get("user_id")
            or payload.get("actor_id")
            or (allowed_users[0] if allowed_users else "")
            or "gateway-test"
        )
        identity = _identity_from_payload(
            {
                "chat_id": chat_id,
                "user_id": user_id,
                "actor_id": str(payload.get("actor_id") or user_id),
                "chat_type": str(payload.get("chat_type") or "dm"),
                "thread_id": payload.get("thread_id"),
            },
            platform=platform,
        )
        session_id = str(
            payload.get("session_id")
            or gateway_session_id(
                platform,
                chat_id=f"test:{channel}",
                user_id=user_id,
            )
        )
        try:
            result = _run_gateway_turn(
                client,
                platform=platform,
                chat_id=chat_id,
                text=text,
                session_id=session_id,
                progress_cfg=cfg,
                identity=identity,
                channel=channel,
                attachments=_attachments_from_inbound_payload(payload),
            )
        except Exception as exc:
            return {
                "ok": False,
                "channel": channel,
                "mode": "agent",
                "error": "agent_turn_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }

        delivery: dict[str, Any] | None = None
        if result.get("reply_text"):
            delivery = _reply_gateway_channel(
                client,
                channel=channel,
                platform=platform,
                cfg=cfg,
                chat_id=chat_id,
                text=str(result.get("reply_text") or ""),
                attachments=list(result.get("attachments") or []),
                context={
                    "kind": "gateway_agent_test",
                    "platform": platform,
                    "chat_id": chat_id,
                    "session_id": result.get("session_id"),
                    "session_key": result.get("session_key"),
                    "user_id": identity.get("user_id") or "",
                    "actor_id": identity.get("actor_id") or "",
                    "thread_id": identity.get("thread_id") or "",
                },
            )

        delivered = True
        if isinstance(delivery, dict):
            delivered = bool(delivery.get("delivered")) and not bool(delivery.get("skipped"))
        return {
            "ok": bool(result.get("ok")) and delivered,
            "channel": channel,
            "mode": "agent",
            "agent": {
                "turn_id": result.get("turn_id"),
                "session_id": result.get("session_id"),
                "session_key": result.get("session_key"),
            },
            "reply_text": result.get("reply_text"),
            "trace_text": result.get("trace_text"),
            "events": result.get("events") or [],
            "delivery": _safe_gateway_delivery(delivery or {}),
        }

    pipe = MessagePipeline(config=client.config)
    try:
        result = pipe.send(channel=channel, text=text, context={"kind": "gateway_test"})
    except Exception as exc:
        return {
            "ok": False,
            "channel": channel,
            "error": "send_failed",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    safe = _safe_gateway_delivery(result)
    return {
        "ok": bool(result.get("delivered")),
        "channel": channel,
        "delivery": safe,
    }


def sync_configured_gateways_on_start(client) -> dict[str, Any]:
    """Synchronize gateway platform runtime affordances for configured channels.

    Telegram menus are intentionally registered by Nerya itself on startup,
    not by an operator script. The command source is the shared gateway command
    registry so `/help`, `/menu`, and Bot API menus stay in lockstep.
    """
    results: list[dict[str, Any]] = []
    for channel, cfg, kind in _configured_gateway_channels(client):
        if kind != "telegram":
            results.append({
                "channel": channel,
                "kind": kind,
                "ok": True,
                "mode": _gateway_runtime_mode(kind, cfg),
                "status": "send_ready",
            })
            continue
        try:
            result = telegram.set_commands(
                channel_cfg=cfg,
                commands=gateway_menu_commands(),
                resolve_secret=_secret_resolver(client),
            )
            safe_result = {k: v for k, v in result.items() if k != "response"}
            results.append({"channel": channel, "kind": "telegram", **safe_result})
        except Exception as exc:
            results.append({
                "channel": channel,
                "kind": "telegram",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
    summary = {
        "ok": all(bool(item.get("ok")) for item in results) if results else True,
        "synced_at": now_iso(),
        "results": results,
    }
    try:
        state = _load_state(client)
        state["startup_sync"] = summary
        _save_state(client, state)
    except Exception:
        pass
    return summary


def launch_configured_gateways_on_start(client) -> dict[str, Any]:
    """Start non-blocking startup sync for gateway platform affordances."""
    configured = _configured_gateway_channels(client)
    if not configured:
        return {"scheduled": False, "reason": "no_configured_gateways"}
    worker = threading.Thread(
        target=sync_configured_gateways_on_start,
        args=(client,),
        name="nerya-gateway-startup-sync",
        daemon=True,
    )
    worker.start()

    # Also bring up long-poll listeners for any channel that opts in
    # (``polling: true`` or ``mode: polling``). With no opt-in flag we
    # default to polling, since a single ``nerya run`` has historically
    # been the operator's "boot everything" command — making them set
    # an extra knob to start receiving messages was the bug.
    poller_started = launch_telegram_pollers(client)
    # If polling was killed by env var (legacy ``-NoTelegramPoller`` flag,
    # or a stale ``NERYA_DISABLE_TELEGRAM_POLLER=1`` in the parent shell),
    # surface that VERY visibly so the operator stops staring at an empty
    # dashboard wondering "why isn't my bot replying?". We log to stderr
    # AND drop a kind=error event into the live ring so /gateway → Live
    # status shows it on first paint.
    has_telegram = any(kind == "telegram" for _, _, kind in configured)
    if has_telegram and _telegram_polling_disabled():
        warning = (
            "[gateway] Telegram polling DISABLED by env "
            "NERYA_DISABLE_TELEGRAM_POLLER=1 — your bot will never reply "
            "to inbound messages. Unset the variable and restart, or run "
            "scripts/windows/start-local.ps1 (the default-on path)."
        )
        try:
            print(warning, file=sys.stderr, flush=True)
        except Exception:
            pass
        for name, _cfg, kind in configured:
            if kind != "telegram":
                continue
            _record_gateway_error(
                platform="telegram",
                channel=str(name),
                reason="polling_disabled_by_env",
                detail="NERYA_DISABLE_TELEGRAM_POLLER=1 is set in the runtime env.",
                hint="Unset the environment variable (or pass nothing to "
                     "scripts/windows/start-local.ps1 — the legacy "
                     "-NoTelegramPoller switch was removed) and restart Nerya.",
            )
    return {
        "scheduled": True,
        "telegram_pollers": poller_started,
        "channels": [name for name, _, _ in configured],
        "gateways": [
            {"channel": name, "kind": kind, "mode": _gateway_runtime_mode(kind, cfg)}
            for name, cfg, kind in configured
        ],
    }


# --------------------------------------------------------- telegram polling
_TELEGRAM_POLLER_THREADS: dict[str, threading.Thread] = {}
_TELEGRAM_POLLER_STOPS: dict[str, threading.Event] = {}
# Channels for which the first poll-tick after startup should drain
# any backlog. The classic failure mode is: poller went offline (env
# var, crash, restart), operator sent N messages waiting for a reply,
# poller comes back online → would otherwise reply to ALL N in order,
# burning rate-limit and sending stale answers. Instead we drop the
# stale ones and only handle the most recent text/callback per channel.
_TELEGRAM_FIRST_POLL_PENDING: set[str] = set()
_TELEGRAM_FIRST_POLL_LOCK = threading.Lock()


def _telegram_polling_disabled() -> bool:
    return os.environ.get("NERYA_DISABLE_TELEGRAM_POLLER", "").strip().lower() in {
        "1", "true", "yes",
    }


def _channel_uses_polling(cfg: dict[str, Any]) -> bool:
    """A channel is poll-driven unless explicitly set to webhook mode."""
    mode = str(cfg.get("mode") or "").strip().lower()
    if mode in ("webhook", "callback"):
        return False
    if mode == "polling":
        return True
    polling = cfg.get("polling")
    if polling is None:
        # Default-on: an operator who configured a Telegram channel and
        # ran ``nerya run`` expects messages to flow without setting a
        # second knob. Webhook deployments must opt out via mode/polling.
        return True
    return bool(polling)


def _telegram_poll_tick(client, channel: str = "telegram") -> dict[str, Any]:
    """Single getUpdates → dispatch tick. Returned shape mirrors the
    HTTP ``/gateway/telegram/poll`` endpoint so they can share probes
    and tests.
    """
    cfg = _telegram_cfg(client, channel)
    state = _load_state(client)
    offset = state.get("offset")
    updates = telegram.get_updates(
        channel_cfg=cfg,
        offset=offset,
        limit=10,
        timeout=25,
        resolve_secret=_secret_resolver(client),
    )
    processed: list[dict[str, Any]] = []
    next_offset = offset
    configured_chat = str(cfg.get("chat_id") or "")

    # First-poll backlog drain: if this is the first tick after the
    # poller (re)started and Telegram replied with multiple buffered
    # updates, keep only the most recent one. This is the difference
    # between "bot was offline for 10 minutes, comes back, replies to
    # all 12 pending messages I sent in frustration" vs. "bot picks up
    # where I left off and answers the latest question only".
    raw_updates = list(updates.get("updates") or [])
    drained_count = 0
    with _TELEGRAM_FIRST_POLL_LOCK:
        is_first_tick = channel in _TELEGRAM_FIRST_POLL_PENDING
        if is_first_tick and updates.get("ok", True):
            _TELEGRAM_FIRST_POLL_PENDING.discard(channel)
    if is_first_tick and len(raw_updates) > 1:
        # Advance the offset past every queued update so they are all
        # acked with Telegram, then keep only the last one for actual
        # dispatch. Callback-button presses are preserved when they
        # are the latest update, otherwise they are dropped along with
        # the stale text backlog.
        for upd in raw_updates[:-1]:
            uid = upd.get("update_id")
            if isinstance(uid, int):
                next_offset = max(int(next_offset or 0), uid + 1)
        drained_count = len(raw_updates) - 1
        raw_updates = raw_updates[-1:]
        _gateway_events_record({
            "platform": "telegram",
            "channel": channel,
            "kind": "info",
            "reason": "backlog_drained",
            "note": f"drained {drained_count} stale message(s) on poller startup; "
                    f"replying only to the latest one",
            "drained_count": drained_count,
        })
    if not updates.get("ok", True):
        # Surface the auth failure once per heartbeat window so the
        # operator sees "your bot_token is invalid" instead of staring
        # at an empty live feed.
        resp = updates.get("response") or {}
        detail = ""
        if isinstance(resp, dict):
            detail = str(resp.get("description") or resp.get("error") or "")
        if not detail:
            detail = str(updates.get("error") or "")
        _record_gateway_error(
            platform="telegram",
            channel=channel,
            reason="poll_failed",
            detail=detail or f"telegram getUpdates failed (status={updates.get('status')})",
            hint="Check the bot_token in /settings → Gateway. Telegram revoked or invalid tokens stop the poller.",
        )
    else:
        _record_gateway_heartbeat(platform="telegram", channel=channel)
    for upd in raw_updates:
        update_id = upd.get("update_id")
        if isinstance(update_id, int):
            next_offset = max(int(next_offset or 0), update_id + 1)
        # Handle inline approval button presses before the regular text
        # path. Forward each callback into the local
        # ``/approvals/callback`` machinery so the dashboard, gateway,
        # and trade engine all see the same source-of-truth resolution.
        cb = upd.get("callback_query") or {}
        if cb:
            actor = cb.get("from") or {}
            msg = cb.get("message") or {}
            chat = msg.get("chat") or {}
            identity = _identity_from_payload(
                {
                    "chat_id": chat.get("id"),
                    "user_id": actor.get("id"),
                    "actor_id": actor.get("username") or actor.get("id"),
                    "thread_id": msg.get("message_thread_id"),
                    "chat_type": chat.get("type"),
                },
                platform="telegram",
            )
            if configured_chat and identity.get("chat_id") != configured_chat:
                _record_gateway_error(
                    platform="telegram",
                    channel=channel,
                    reason="chat_not_allowed",
                    chat_id=str(identity.get("chat_id") or ""),
                    user_id=str(identity.get("user_id") or ""),
                    detail=f"callback from chat_id={identity.get('chat_id')}, "
                           f"configured chat_id={configured_chat}",
                    hint="The configured chat_id only matches one chat. "
                         "If you added the bot to a group, the chat_id is the negative group id "
                         "(use @userinfobot in the group, or clear chat_id to accept any chat).",
                )
                processed.append({
                    "ok": False,
                    "kind": "callback_query",
                    "chat_id": identity.get("chat_id") or "",
                    "error": "chat_not_allowed",
                })
                continue
            allowed, reason = _gateway_actor_allowed(cfg, identity)
            if not allowed:
                _record_gateway_error(
                    platform="telegram",
                    channel=channel,
                    reason=reason or "unauthorized",
                    chat_id=str(identity.get("chat_id") or ""),
                    user_id=str(identity.get("user_id") or ""),
                    detail="callback rejected by allow/deny list",
                    hint="Edit /settings → Gateway → allowed_user_ids / denied_user_ids "
                         "(blank means accept anyone).",
                )
                processed.append({
                    "ok": False,
                    "kind": "callback_query",
                    "error": "unauthorized",
                    "reason": reason,
                })
                continue
            try:
                processed.append(
                    _handle_telegram_callback(client, cfg, cb)
                )
            except Exception as exc:
                processed.append({
                    "ok": False, "kind": "callback_query",
                    "error": f"{type(exc).__name__}: {exc}",
                })
            continue
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        actor = msg.get("from") or {}
        identity = _identity_from_payload(
            {
                "chat_id": chat_id,
                "user_id": actor.get("id"),
                "actor_id": actor.get("username") or actor.get("id"),
                "thread_id": msg.get("message_thread_id"),
                "chat_type": chat.get("type"),
            },
            platform="telegram",
        )
        text = str(first_present([msg.get("text"), msg.get("caption")]) or "")
        raw_attachments = _attachments_from_inbound_payload({"message": msg})
        if not chat_id or (not text and not raw_attachments):
            continue
        if configured_chat and chat_id != configured_chat:
            _record_gateway_error(
                platform="telegram",
                channel=channel,
                reason="chat_not_allowed",
                chat_id=chat_id,
                user_id=str(identity.get("user_id") or ""),
                detail=f"received chat_id={chat_id}, configured chat_id={configured_chat}",
                hint="The configured chat_id only matches one chat. "
                     "If you added the bot to a group, the chat_id is the negative "
                     "group id (use @userinfobot in the group). "
                     "Or clear chat_id in /settings → Gateway to accept any chat.",
            )
            processed.append({"ok": False, "chat_id": chat_id,
                              "error": "chat_not_allowed"})
            continue
        allowed, reason = _gateway_actor_allowed(cfg, identity)
        if not allowed:
            _record_gateway_error(
                platform="telegram",
                channel=channel,
                reason=reason or "unauthorized",
                chat_id=chat_id,
                user_id=str(identity.get("user_id") or ""),
                detail="message rejected by allow/deny list",
                hint="Edit /settings → Gateway → allowed_user_ids / denied_user_ids "
                     "(blank means accept anyone).",
            )
            processed.append({
                "ok": False,
                "chat_id": chat_id,
                "user_id": identity.get("user_id") or "",
                "error": "unauthorized",
                "reason": reason,
            })
            continue
        attachments = _prepare_inbound_attachments(
            client,
            {"message": msg, "update_id": update_id},
            platform="telegram",
            cfg=cfg,
            upload_id=f"telegram_{update_id or msg.get('message_id') or int(time.time() * 1000)}",
        ) if raw_attachments else []
        if not text.strip() and attachments:
            text = "Please review the attached file(s)."
        try:
            if attachments:
                processed.append(
                    _handle_text(
                        client,
                        cfg,
                        chat_id,
                        text,
                        update_id,
                        identity,
                        attachments=attachments,
                    )
                )
            else:
                processed.append(
                    _handle_text(client, cfg, chat_id, text, update_id, identity)
                )
        except Exception as exc:
            _record_gateway_error(
                platform="telegram",
                channel=channel,
                reason="handler_failed",
                chat_id=chat_id,
                user_id=str(identity.get("user_id") or ""),
                detail=f"{type(exc).__name__}: {exc}",
                hint="See nerya logs for the full traceback.",
            )
            processed.append({"ok": False, "chat_id": chat_id,
                              "error": f"{type(exc).__name__}: {exc}"})
    if next_offset is not None:
        state["offset"] = next_offset
        _save_state(client, state)
    return {
        "ok": bool(updates.get("ok", True)),
        "processed": processed,
        "offset": next_offset,
    }


def _poll_loop(client, channel: str, stop: threading.Event,
               *, interval_s: float = 1.0) -> None:
    backoff = interval_s
    while not stop.is_set():
        try:
            result = _telegram_poll_tick(client, channel)
            backoff = interval_s if result.get("ok") else min(backoff * 2, 60.0)
        except Exception:
            backoff = min(backoff * 2, 60.0)
        stop.wait(backoff)


def launch_telegram_pollers(client) -> list[str]:
    """Spawn a daemon thread per polling-enabled telegram channel.

    Idempotent: a second call is a no-op for any channel whose worker
    is already alive. Returns the list of channels for which a poller
    is running after this call.
    """
    if _telegram_polling_disabled():
        return []
    started: list[str] = []
    for channel, cfg in _configured_telegram_channels(client):
        if not _channel_uses_polling(cfg):
            continue
        existing = _TELEGRAM_POLLER_THREADS.get(channel)
        if existing is not None and existing.is_alive():
            started.append(channel)
            continue
        stop = threading.Event()
        thread = threading.Thread(
            target=_poll_loop,
            args=(client, channel, stop),
            name=f"nerya-telegram-poll-{channel}",
            daemon=True,
        )
        # Mark this channel for backlog drain on its very first tick
        # so we don't reply to a queue of messages the operator sent
        # while polling was disabled (env var, crash, restart).
        with _TELEGRAM_FIRST_POLL_LOCK:
            _TELEGRAM_FIRST_POLL_PENDING.add(channel)
        _TELEGRAM_POLLER_THREADS[channel] = thread
        _TELEGRAM_POLLER_STOPS[channel] = stop
        thread.start()
        started.append(channel)
    return started


def stop_telegram_pollers() -> None:
    """Signal every polling worker to exit on the next iteration. Used
    by tests + any future graceful-shutdown hook.
    """
    for stop in _TELEGRAM_POLLER_STOPS.values():
        stop.set()
    _TELEGRAM_POLLER_THREADS.clear()
    _TELEGRAM_POLLER_STOPS.clear()
    # Re-arm backlog drain for the next start. Without this a quick
    # stop+start cycle (e.g. tests, hot-reload) would skip the drain
    # and reply to whatever Telegram still has queued.
    with _TELEGRAM_FIRST_POLL_LOCK:
        _TELEGRAM_FIRST_POLL_PENDING.clear()


def gateway_runtime_status(client) -> dict[str, Any]:
    """Return operator-safe gateway startup state.

    The status intentionally reports whether secret references are present,
    never their values. It is used by the Windows launcher and the dashboard
    to distinguish "not configured" from "configured but not running".
    """
    doc = yaml_io.load(client.config.paths.messages_channels, default={}) or {}
    raw_channels = doc.get("channels") if isinstance(doc, dict) else {}
    channels = raw_channels if isinstance(raw_channels, dict) else {}
    state = _load_state(client)
    telegram_channels: list[dict[str, Any]] = []
    gateway_channels: list[dict[str, Any]] = []
    configured_count = 0
    poller_count = 0
    for name, raw_cfg in sorted(channels.items()):
        cfg = dict(raw_cfg or {})
        kind = _gateway_kind(str(name), cfg)
        if kind in {"dashboard", "local"}:
            continue
        configured = _gateway_channel_configured(kind, cfg)
        if configured:
            configured_count += 1
        base_status = {
            "channel": str(name),
            "kind": kind,
            "configured": configured,
            "mode": _gateway_runtime_mode(kind, cfg),
            "outbound_ready": configured,
        }
        if kind == "telegram":
            token_ref = cfg.get("bot_token_ref") or cfg.get("token_ref")
            polling_enabled = configured and _channel_uses_polling(cfg)
            thread = _TELEGRAM_POLLER_THREADS.get(str(name))
            poller_alive = bool(thread is not None and thread.is_alive())
            if poller_alive:
                poller_count += 1
            mode = str(cfg.get("mode") or ("polling" if polling_enabled else "webhook"))
            telegram_status = {
                **base_status,
                "kind": "telegram",
                "bot_token_ref_configured": bool(token_ref),
                "chat_id_configured": bool(cfg.get("chat_id") or cfg.get("chat_id_ref")),
                "polling_enabled": polling_enabled,
                "poller_alive": poller_alive,
                "mode": mode,
            }
            telegram_channels.append(telegram_status)
            gateway_channels.append(telegram_status)
            continue
        gateway_channels.append({
            **base_status,
            "webhook_configured": bool(
                cfg.get("webhook_url")
                or cfg.get("webhook_url_ref")
                or cfg.get("incoming_webhook_url")
                or cfg.get("incoming_webhook_url_ref")
                or cfg.get("url")
                or cfg.get("url_ref")
            ),
            "status_webhook_configured": bool(
                cfg.get("status_webhook_url")
                or cfg.get("status_webhook_url_ref")
                or cfg.get("status_url")
            ),
        })
    return {
        "ok": True,
        "channels_file_exists": client.config.paths.messages_channels.exists(),
        "configured_gateway_count": configured_count,
        "gateways": {
            "configured_count": configured_count,
            "channels": gateway_channels,
            "startup_sync": state.get("startup_sync") if isinstance(state, dict) else None,
        },
        "telegram": {
            "polling_disabled_by_env": _telegram_polling_disabled(),
            "poller_count": poller_count,
            "channels": telegram_channels,
            "startup_sync": state.get("startup_sync") if isinstance(state, dict) else None,
            "offset": state.get("offset") if isinstance(state, dict) else None,
        },
    }


def _state_path(client):
    return client.config.paths.messages_dir / "telegram_gateway.yml"


def _load_state(client) -> dict[str, Any]:
    return yaml_io.load(_state_path(client), default={}) or {}


def _save_state(client, state: dict[str, Any]) -> None:
    path = _state_path(client)
    current = yaml_io.load(path, default={}) or {}
    merged = {**current, **state} if isinstance(current, dict) else dict(state)
    text = yaml_io.dumps(merged) if hasattr(yaml_io, "dumps") else ""
    if text:
        atomic_write_text(path, text)
    else:
        yaml_io.dump(path, merged)


def _delete_session(client, session_id: str) -> None:
    """Best-effort session reset used by the gateway ``/new`` command."""

    if not session_id:
        return
    try:
        from ..agent.session import SessionStore

        SessionStore(client.config.paths.root).delete(session_id)
    except Exception:
        pass
    # Also drop the per-session MCP describe cache so a
    # fresh chat doesn't inherit stale "described" state from the
    # previous session_id. Best-effort; missing module / missing key
    # is fine.
    try:
        from ..mcp.lazy import reset_session_cache

        reset_session_cache(
            workspace_root=client.config.paths.root,
            session_id=session_id,
        )
    except Exception:
        pass


def _reply(
    client,
    cfg: dict[str, Any],
    chat_id: str,
    text: str,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    msg = {
        "message_id": gateway_message_id("telegram", chat_id=chat_id, direction="reply"),
        "channel": "telegram",
        "kind": "telegram",
        "text": text,
        "ts": now_iso(),
        "delivered": False,
        "rate_limited": False,
        "attachments": list(attachments or []),
    }
    channel_cfg = dict(cfg)
    channel_cfg["chat_id"] = str(chat_id)
    path = telegram.send(
        client.config.paths.outbox_messages,
        msg,
        channel_cfg=channel_cfg,
        resolve_secret=_secret_resolver(client),
    )
    msg["outbox_path"] = str(path)
    return msg


def _typing(client, cfg: dict[str, Any], chat_id: str) -> None:
    try:
        telegram.send_chat_action(
            channel_cfg=cfg,
            chat_id=str(chat_id),
            action="typing",
            resolve_secret=_secret_resolver(client),
        )
    except Exception:
        pass


def _typing_until_done(client, cfg: dict[str, Any], chat_id: str, stop: threading.Event) -> None:
    while not stop.is_set():
        _typing(client, cfg, chat_id)
        stop.wait(4.0)


def _send_progress(client, cfg: dict[str, Any], chat_id: str, text: str | None) -> None:
    if not text:
        return
    try:
        _typing(client, cfg, chat_id)
        _reply(client, cfg, chat_id, text)
    except Exception:
        pass


def _handle_telegram_callback(client, cfg: dict[str, Any],
                              cb: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a Telegram inline-keyboard button press.

    The expected ``callback_data`` shape is the one produced by
    :func:`messaging.approval_prompts.build_prompt` — i.e.
    ``approve:<approval_id>`` / ``reject:<approval_id>`` /
    ``details:<approval_id>``. We forward the press into the same
    ``/approvals/callback`` plumbing the dashboard already uses, then
    answer the callback query so the spinner stops on the user's
    phone, and finally edit the original message so it stops looking
    actionable.
    """
    from ..messaging.approval_prompts import (
        parse_callback_data,
        resolve_callback,
    )
    from . import routes_approvals as _ra

    callback_data = str(cb.get("data") or "")
    callback_query_id = str(cb.get("id") or "")
    actor = cb.get("from") or {}
    actor_id = str(
        actor.get("username") or actor.get("id") or "telegram"
    )
    msg = cb.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    message_id = msg.get("message_id")

    action, aid = parse_callback_data(callback_data)
    if not action or not aid:
        # Acknowledge so Telegram clears the spinner even on garbage.
        try:
            telegram.answer_callback_query(
                channel_cfg=cfg,
                callback_query_id=callback_query_id,
                text="Unknown action",
                resolve_secret=_secret_resolver(client),
            )
        except Exception:
            pass
        return {
            "ok": False, "kind": "callback_query",
            "callback_data": callback_data,
            "error": "callback_data not recognized",
        }

    rec = _ra._find_record(client, aid)

    moved_state = {"state": None}

    def _approve(target_id: str) -> None:
        moved = _ra._move_record(
            client, target_id, state="approved",
            note=f"approved via telegram by {actor_id}",
        )
        moved_state["state"] = "approved" if moved else None

    def _reject(target_id: str, reason: str) -> None:
        moved = _ra._move_record(
            client, target_id, state="rejected", note=reason,
        )
        moved_state["state"] = "rejected" if moved else None

    record_actor = str((rec or {}).get("actor_id") or "")

    def actor_owns(req_actor: str, _approval_id: str) -> bool:
        if not record_actor:
            return True
        return req_actor == record_actor

    resolution = resolve_callback(
        callback_data,
        actor_id=actor_id,
        approve=_approve,
        reject=_reject,
        actor_owns=actor_owns,
    )

    # Acknowledge the button press immediately.
    if resolution.state == "approved":
        ack_text = "✅ Approved"
    elif resolution.state == "rejected":
        ack_text = "❌ Rejected"
    elif resolution.state == "details":
        ack_text = "ℹ Details"
    elif resolution.state == "error":
        ack_text = f"⚠ {resolution.note or 'error'}"
    else:
        ack_text = "ignored"
    try:
        telegram.answer_callback_query(
            channel_cfg=cfg,
            callback_query_id=callback_query_id,
            text=ack_text,
            resolve_secret=_secret_resolver(client),
        )
    except Exception:
        pass

    # Strip the inline keyboard so the same approval cannot be
    # double-clicked from the same chat.
    if message_id is not None and chat_id and resolution.state in {
        "approved", "rejected",
    }:
        try:
            telegram.clear_reply_markup(
                channel_cfg=cfg,
                chat_id=chat_id,
                message_id=message_id,
                resolve_secret=_secret_resolver(client),
            )
        except Exception:
            pass
        try:
            _ra._retract_approval_cards(
                client, aid, state=str(resolution.state),
            )
        except Exception:
            pass
        try:
            _ra._publish_approval_resolution(
                client,
                aid,
                state=str(resolution.state),
                record=rec,
            )
        except Exception:
            pass

    # Audit log alongside the dashboard's own callback log.
    try:
        from ..core import jsonl as _jsonl
        from ..core.time import now_iso as _now_iso
        _jsonl.append(
            client.config.paths.approvals_pending.parent
            / "callbacks.jsonl",
            {
                "approval_id": aid,
                "action": action,
                "actor_id": actor_id,
                "platform": "telegram",
                "chat_id": chat_id,
                "state": resolution.state,
                "ts": _now_iso(),
            },
        )
    except Exception:
        pass

    return {
        "ok": resolution.state in {"approved", "rejected", "details"},
        "kind": "callback_query",
        "callback_data": callback_data,
        "approval_id": aid,
        "action": action,
        "state": resolution.state,
        "actor_id": actor_id,
    }


def _handle_text(client, cfg: dict[str, Any], chat_id: str, text: str,
                 update_id: int | None = None,
                 identity: dict[str, str] | None = None,
                 attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    channel_cfg = {**cfg, "kind": "telegram", "chat_id": str(chat_id)}
    result = _run_gateway_turn(
        client,
        platform="telegram",
        chat_id=str(chat_id),
        text=(text or "").strip(),
        progress_cfg=channel_cfg,
        identity=identity,
        channel="telegram",
        attachments=attachments,
        update_id=update_id,
        auto_reply=True,
    )
    delivery = result.get("delivery") or {}
    if delivery.get("delivered") is False:
        _record_gateway_error(
            platform="telegram",
            channel="telegram",
            reason="reply_not_delivered",
            chat_id=str(chat_id),
            detail=str(
                delivery.get("delivery_note") or delivery.get("error") or "send failed"
            ),
            hint="Telegram returned a non-2xx. Verify your bot is still a member of "
                 "the chat and the bot_token is current.",
        )
    return result


def _format_capture_notice(captures) -> str:
    if not captures:
        return ""
    lines = [
        f"detected {len(captures)} secret value(s) in your message — they were "
        "stripped before reaching the AI.",
    ]
    for cap in captures:
        lines.append(
            f"  - {cap.kind}: token={cap.token} preview={cap.preview} "
            f"(expires in {cap.ttl_s // 60}m)"
        )
    lines.append(
        "Use /accounts intake to bind these tokens to a credential field, or "
        "they will auto-expire."
    )
    return "\n".join(lines)


def _channel_cfg(client, channel: str, platform: str | None = None) -> dict[str, Any]:
    doc = yaml_io.load(client.config.paths.messages_channels, default={}) or {}
    cfg = dict((doc.get("channels") or {}).get(channel) or {})
    if platform and not cfg.get("kind"):
        cfg["kind"] = platform
    return cfg


def _emit_status(client, cfg: dict[str, Any], text: str, *,
                 telegram_text: bool = False) -> None:
    try:
        kind = str(cfg.get("kind") or "")
        if kind == "telegram" and cfg.get("chat_id"):
            if telegram_text:
                _send_progress(client, cfg, str(cfg.get("chat_id")), text)
            else:
                _typing(client, cfg, str(cfg.get("chat_id")))
            return
        generic_platform.send_status(
            channel_cfg=cfg,
            text=text,
            resolve_secret=_secret_resolver(client),
        )
    except Exception:
        pass


def _attachment_reply_text(attachments: list[dict[str, Any]]) -> str:
    names = [
        str(item.get("name") or item.get("filename") or item.get("mime_type") or "attachment")
        for item in attachments
        if isinstance(item, dict)
    ]
    if not names:
        return "Attachment returned."
    return "Attachment returned: " + ", ".join(names[:5])


def _reply_gateway_channel(client, *, channel: str, platform: str,
                           cfg: dict[str, Any], chat_id: str,
                           text: str,
                           attachments: list[dict[str, Any]] | None = None,
                           context: dict[str, Any] | None = None) -> dict[str, Any]:
    attachments = list(attachments or [])
    if not text.strip() and attachments:
        text = _attachment_reply_text(attachments)
    if not text.strip():
        return {"skipped": True, "reason": "empty_text"}
    kind = _gateway_kind(channel, cfg) if cfg else platform
    if kind == "telegram":
        if not chat_id:
            return {"skipped": True, "reason": "chat_id_required"}
        return _safe_gateway_delivery(_reply(client, cfg, chat_id, text, attachments))
    if not _gateway_channel_configured(kind, cfg):
        return {
            "skipped": True,
            "reason": "channel_not_configured",
            "channel": channel,
            "kind": kind,
        }
    pipe = MessagePipeline(config=client.config)
    return _safe_gateway_delivery(
        pipe.send(
            channel=channel,
            text=text,
            context=context,
            attachments=attachments,
        )
    )


def _run_gateway_turn(client, *, platform: str, chat_id: str, text: str,
                      session_id: str | None = None,
                      progress_cfg: dict[str, Any] | None = None,
                      identity: dict[str, str] | None = None,
                      channel: str | None = None,
                      attachments: list[dict[str, Any]] | None = None,
                      update_id: int | None = None,
                      auto_reply: bool = False) -> dict[str, Any]:
    attachments = list(attachments or [])
    state = _load_state(client)
    active_sessions = state.get("active_sessions") if isinstance(state, dict) else {}
    if not isinstance(active_sessions, dict):
        active_sessions = {}
    cfg = progress_cfg or {"kind": platform}
    identity = identity or _identity_from_payload(
        {"chat_id": chat_id},
        platform=platform,
    )
    session_key, session = _session_id_for_identity(
        platform,
        identity,
        cfg,
        active_sessions,
        explicit_session_id=session_id,
    )
    channel_name = (
        str(channel)
        if channel
        else (str(progress_cfg.get("channel")) if isinstance(progress_cfg, dict) and progress_cfg.get("channel") else platform)
    )

    if text.strip().startswith("/"):
        # Surface the slash-command interaction in the dashboard live
        # stream too — operators want to see what their bot is responding
        # to, even when the agent didn't run a full turn.
        _gateway_events_record(
            {
                "kind": "inbound",
                "platform": platform,
                "channel": channel_name,
                "chat_id": str(chat_id),
                "user_id": identity.get("user_id") or "",
                "actor_id": identity.get("actor_id") or "",
                "session_id": session,
                "session_key": session_key,
                "text": text[:280],
                "command": True,
            }
        )
        outcome = GATEWAY_COMMAND_REGISTRY.handle(
            text,
            CommandContext(
                client=client,
                platform=platform,
                chat_id=str(chat_id),
                session_id=session,
                raw_text=text,
                session_key=session_key,
                user_id=identity.get("user_id") or "",
                thread_id=identity.get("thread_id") or "",
                state=state,
                save_state=lambda new_state: _save_state(client, dict(new_state)),
                delete_session=lambda sid: _delete_session(client, sid),
                dashboard_url=resolve_dashboard_url(client.config),
            ),
        )
        if outcome.handled:
            delivery = None
            if auto_reply and outcome.reply_text:
                delivery = _reply_gateway_channel(
                    client,
                    channel=channel_name,
                    platform=platform,
                    cfg=cfg,
                    chat_id=str(chat_id),
                    text=outcome.reply_text,
                )
            outbound_event = {
                "kind": "outbound",
                "platform": platform,
                "channel": channel_name,
                "chat_id": str(chat_id),
                "session_id": session,
                "session_key": session_key,
                "command": str(outcome.command or ""),
                "text": (outcome.reply_text or "")[:280],
            }
            if delivery is not None:
                outbound_event["delivered"] = bool(delivery.get("delivered", False))
            _gateway_events_record(
                outbound_event
            )
            response = {
                "ok": True,
                "platform": platform,
                "chat_id": str(chat_id),
                "session_id": session,
                "session_key": session_key,
                "command": outcome.command,
                "reply_text": outcome.reply_text,
            }
            if delivery is not None:
                response["delivery"] = delivery
            return response

    kernel = AgentKernel(config=client.config, skills=client.skills)
    seen_phase_text: set[str] = set()

    def emit(ctx) -> None:
        status = hook_status_text(ctx.phase, ctx.data, ctx.iteration)
        if not status:
            return
        if status not in seen_phase_text:
            seen_phase_text.add(status)
            _emit_status(client, cfg, status, telegram_text=auto_reply)
        # Always mirror to live-events ring so the dashboard can subscribe
        # even when no status_webhook_url is configured. This is what gives
        # Slack/Feishu/Discord the same live "agent is thinking…"
        # surface that previously only Telegram had.
        _gateway_events_record(
            {
                "kind": "phase",
                "platform": platform,
                "channel": channel_name,
                "chat_id": str(chat_id),
                "session_id": session,
                "session_key": session_key,
                "phase": str(ctx.phase),
                "iteration": int(getattr(ctx, "iteration", 0) or 0),
                "text": status,
            }
        )

    for phase in (
        "after_plan", "after_subagents", "before_think", "after_think",
        "after_act", "after_observe", "before_close",
    ):
        try:
            kernel.hooks.register(phase, emit)
        except Exception:
            pass

    scan = scan_and_redact(text, buffer=get_default_buffer())
    redacted = scan.redacted_text
    captured_notice = ""
    if scan.captured:
        captured_notice = _format_capture_notice(scan.captures)
        if captured_notice:
            try:
                _emit_status(
                    client, cfg, captured_notice, telegram_text=auto_reply
                )
            except Exception:
                pass

    inbound_payload = {
        "text": redacted,
        "session_key": session_key,
        "user_id": identity.get("user_id") or "",
        "actor_id": identity.get("actor_id") or "",
        "thread_id": identity.get("thread_id") or "",
        "chat_type": identity.get("chat_type") or "",
        "secrets_captured": len(scan.captures),
        "attachments": attachments,
    }
    inbound_event = {
        "kind": "inbound",
        "platform": platform,
        "channel": channel_name,
        "chat_id": str(chat_id),
        "user_id": identity.get("user_id") or "",
        "actor_id": identity.get("actor_id") or "",
        "session_id": session,
        "session_key": session_key,
        "text": redacted[:280],
        "attachments": attachments,
    }
    if update_id is not None:
        inbound_payload["update_id"] = update_id
        inbound_event["update_id"] = update_id
    GatewayMirror(client.config.paths).record_inbound(
        channel=platform,
        handle=str(chat_id),
        session_id=session,
        payload=inbound_payload,
    )
    _gateway_events_record(inbound_event)
    stop_typing = None
    if auto_reply and platform == "telegram":
        stop_typing = threading.Event()
        threading.Thread(
            target=_typing_until_done,
            args=(client, cfg, chat_id, stop_typing),
            daemon=True,
        ).start()
    try:
        result = kernel.run_turn(
            trigger={
                "source": platform,
                "kind": "user.chat",
                "target": "main",
                "payload": {
                    "text": redacted,
                    "channel": platform,
                    "chat_id": str(chat_id),
                    "session_key": session_key,
                    "user_id": identity.get("user_id") or "",
                    "actor_id": identity.get("actor_id") or "",
                    "thread_id": identity.get("thread_id") or "",
                    "chat_type": identity.get("chat_type") or "",
                    "secret_tokens": [c.token for c in scan.captures],
                    "secret_kinds": [c.kind for c in scan.captures],
                    "attachments": attachments,
                },
            },
            session_id=session,
        )
    finally:
        if stop_typing is not None:
            stop_typing.set()
    reply = agent_reply_text(result)
    result_attachments = list(getattr(result, "attachments", []) or [])
    events = turn_events(result)
    trace_text = compact_turn_summary(result)
    delivery = None
    if auto_reply:
        if platform == "telegram" and trace_text:
            _reply_gateway_channel(
                client,
                channel=channel_name,
                platform=platform,
                cfg=cfg,
                chat_id=str(chat_id),
                text=trace_text,
            )
        delivery = _reply_gateway_channel(
            client,
            channel=channel_name,
            platform=platform,
            cfg=cfg,
            chat_id=str(chat_id),
            text=reply,
            attachments=result_attachments,
        )
    outbound_payload = {
        "text": reply,
        "events": events,
        "attachments": result_attachments,
    }
    outbound_event = {
        "kind": "outbound",
        "platform": platform,
        "channel": channel_name,
        "chat_id": str(chat_id),
        "session_id": session,
        "session_key": session_key,
        "turn_id": result.turn_id,
        "text": (reply or "")[:280],
        "attachments": result_attachments,
    }
    if delivery is not None:
        outbound_payload["delivery"] = delivery
        outbound_event["delivered"] = bool(delivery.get("delivered", False))
    GatewayMirror(client.config.paths).record_outbound(
        channel=platform,
        handle=str(chat_id),
        session_id=session,
        payload=outbound_payload,
    )
    _gateway_events_record(outbound_event)
    state = _load_state(client)
    active_sessions = state.get("active_sessions") if isinstance(state, dict) else {}
    if not isinstance(active_sessions, dict):
        active_sessions = {}
    active_sessions[session_key] = session
    state["active_sessions"] = active_sessions
    state["last_turn_id"] = result.turn_id
    state["last_trace"] = trace_text
    _save_state(client, state)
    response = {
        "ok": True,
        "platform": platform,
        "chat_id": str(chat_id),
        "session_id": session,
        "session_key": session_key,
        "turn_id": result.turn_id,
        "reply_text": reply,
        "attachments": result_attachments,
        "trace_text": trace_text,
        "events": events,
        "secrets_captured": [capture.asdict() for capture in scan.captures],
        "captured_notice": captured_notice,
    }
    if delivery is not None:
        response["delivery"] = delivery
    return response


def routes():
    def platforms(client, _payload):
        return {"platforms": list_platforms()}

    def gateway_config(client, _payload):
        return gateway_config_snapshot(client)

    def gateway_config_save(client, payload):
        try:
            return gateway_config_upsert(client, payload if isinstance(payload, dict) else {})
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def gateway_config_remove(client, payload):
        try:
            return gateway_config_delete(client, payload if isinstance(payload, dict) else {})
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def gateway_config_probe(client, payload):
        try:
            return gateway_config_test(client, payload if isinstance(payload, dict) else {})
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def gateway_status(client, _payload):
        return gateway_runtime_status(client)

    def gateway_commands(client, payload):
        platform = str((payload or {}).get("platform") or (payload or {}).get("kind") or "").lower()
        spec = get_platform(platform) if platform else None
        platform_id = spec.id if spec else None
        catalog = _gateway_command_catalog(platform_id)
        return {
            "ok": True,
            "platform": platform_id or "all",
            **catalog,
        }

    def gateway_inbound(client, payload):
        payload = payload if isinstance(payload, dict) else {}
        platform = str(payload.get("platform") or payload.get("kind") or "webhook").lower()
        spec = require_platform(platform)
        channel = str(payload.get("channel") or spec.id)
        cfg = _channel_cfg(client, channel, spec.id)
        identity_payload = _identity_payload_from_inbound(payload)
        identity = _identity_from_payload(identity_payload, platform=spec.id)
        chat_id = identity.get("chat_id") or "default"
        if chat_id and not cfg.get("chat_id"):
            cfg["chat_id"] = chat_id
        allowed, reason = _gateway_actor_allowed(cfg, identity)
        if not allowed:
            _record_gateway_error(
                platform=spec.id,
                channel=channel,
                reason=reason or "unauthorized",
                chat_id=chat_id,
                user_id=str(identity.get("user_id") or ""),
                detail="inbound rejected by allow/deny list",
                hint="Edit /settings → Gateway → allowed_user_ids / denied_user_ids "
                     "(blank means accept anyone).",
            )
            return {
                "ok": False,
                "error": "unauthorized",
                "reason": reason,
                "platform": spec.id,
                "channel": channel,
                "chat_id": chat_id,
                "user_id": identity.get("user_id") or "",
            }

        cb = payload.get("callback_query") if isinstance(payload.get("callback_query"), dict) else {}
        if cb and spec.id == "telegram":
            return _handle_telegram_callback(client, cfg, cb)
        callback_data = _callback_data_from_payload(payload)
        if callback_data:
            return _handle_gateway_approval_callback(
                client,
                platform=spec.id,
                channel=channel,
                identity=identity,
                callback_data=callback_data,
            )

        text = _text_from_inbound_payload(payload)
        attachments = _prepare_inbound_attachments(
            client,
            payload,
            platform=spec.id,
            cfg=cfg,
            upload_id=_inbound_upload_id(spec.id, payload),
        )
        if not text.strip() and not attachments:
            return {"ok": False, "error": "text required", "platform": spec.id}
        if not text.strip():
            text = "Please review the attached file(s)."
        result = _run_gateway_turn(
            client, platform=spec.id, chat_id=chat_id, text=text,
            session_id=payload.get("session_id"), progress_cfg=cfg,
            identity=identity, channel=channel, attachments=attachments,
        )
        auto_reply = payload.get("auto_reply")
        if auto_reply is None:
            auto_reply = cfg.get("auto_reply", True)
        if auto_reply is not False and result.get("reply_text"):
            delivery = _reply_gateway_channel(
                client,
                channel=channel,
                platform=spec.id,
                cfg=cfg,
                chat_id=chat_id,
                text=str(result.get("reply_text") or ""),
                attachments=list(result.get("attachments") or []),
                context={
                    "kind": "gateway_auto_reply",
                    "platform": spec.id,
                    "chat_id": chat_id,
                    "session_id": result.get("session_id"),
                    "session_key": result.get("session_key"),
                    "user_id": identity.get("user_id") or "",
                    "actor_id": identity.get("actor_id") or "",
                    "thread_id": identity.get("thread_id") or "",
                },
            )
            result["delivery"] = delivery
            if isinstance(delivery, dict):
                if delivery.get("skipped"):
                    _record_gateway_error(
                        platform=spec.id,
                        channel=channel,
                        reason=str(delivery.get("reason") or "skipped"),
                        chat_id=chat_id,
                        detail="auto-reply skipped",
                        hint="Confirm the channel is fully configured under "
                             "/settings → Gateway (every required field set).",
                    )
                elif delivery.get("delivered") is False:
                    _record_gateway_error(
                        platform=spec.id,
                        channel=channel,
                        reason="reply_not_delivered",
                        chat_id=chat_id,
                        detail=str(delivery.get("delivery_note") or delivery.get("error") or "send failed"),
                        hint="The platform returned a non-2xx. Verify webhook URL / app credentials.",
                    )
        return result

    def gateway_send(client, payload):
        platform = str(payload.get("platform") or payload.get("channel") or "dashboard").lower()
        spec = get_platform(platform)
        if spec is None:
            return {"ok": False, "error": f"unknown platform: {platform}"}
        channel = str(payload.get("channel") or spec.id)
        pipe = MessagePipeline(config=client.config)
        return pipe.send(
            channel=channel,
            text=str(payload.get("text") or payload.get("message") or ""),
            strategy_id=payload.get("strategy_id"),
            context=payload.get("context") if isinstance(payload.get("context"), dict) else None,
        )

    def telegram_setup(client, payload):
        channel = payload.get("channel") or "telegram"
        cfg = _telegram_cfg(client, channel)
        commands = payload.get("commands") or gateway_menu_commands()
        return telegram.set_commands(
            channel_cfg=cfg,
            commands=commands,
            resolve_secret=_secret_resolver(client),
        )

    def telegram_poll(client, payload):
        # Manual one-shot poll. The background long-poller handles
        # auto-dispatch; this endpoint is preserved for the dashboard
        # "run a poll right now" button + smoke tests + operators
        # debugging webhook regressions.
        channel = payload.get("channel") or "telegram"
        return _telegram_poll_tick(client, channel)

    def telegram_send(client, payload):
        cfg = _telegram_cfg(client, payload.get("channel") or "telegram")
        chat_id = str(payload.get("chat_id") or cfg.get("chat_id") or "")
        if not chat_id:
            return {"ok": False, "error": "chat_id required"}
        text = str(payload.get("text") or "")
        return _reply(client, cfg, chat_id, text)

    def telegram_diagnose(client, payload):
        # Operator-driven probe — runs `getMe` to verify the bot_token,
        # then `getChat` to verify the configured chat is one the bot
        # can actually reach. Used by the dashboard's "Diagnose" button
        # so an operator can resolve "configured but no replies"
        # without poking around in the logs.
        body = payload if isinstance(payload, dict) else {}
        channel = str(body.get("channel") or "telegram")
        out = diagnose_telegram_gateway(
            client.config.paths,
            channel=channel,
            chat_id=body.get("chat_id"),
        )
        # Live runtime info — is the poller up? When was the last poll?
        thread = _TELEGRAM_POLLER_THREADS.get(channel)
        state = _load_state(client)
        out["polling"] = {
            "alive": bool(thread is not None and thread.is_alive()),
            "disabled_by_env": _telegram_polling_disabled(),
            "offset": state.get("offset") if isinstance(state, dict) else None,
        }
        return out

    def gateway_events(client, payload):
        # Cursor-based polling endpoint. The dashboard polls this at ~1.5s
        # intervals with the latest ``since`` cursor; the response gives
        # the new ring-buffer slice plus the next cursor. This is the
        # shared live-status surface and works uniformly across
        # every gateway platform — Telegram polling, Slack/Feishu/WeCom
        # webhook, Discord interactions, etc.
        body = payload if isinstance(payload, dict) else {}
        try:
            since = int(body.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        try:
            limit = int(body.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        channel = body.get("channel") if isinstance(body.get("channel"), str) else None
        platform = body.get("platform") if isinstance(body.get("platform"), str) else None
        if isinstance(channel, list) and channel:
            channel = channel[0]
        if isinstance(platform, list) and platform:
            platform = platform[0]
        events = _gateway_events_snapshot(
            since=since,
            channel=channel if isinstance(channel, str) else None,
            platform=platform if isinstance(platform, str) else None,
            limit=max(1, min(500, limit)),
        )
        cursor = events[-1]["seq"] if events else _gateway_event_cursor()
        return {
            "ok": True,
            "events": events,
            "cursor": cursor,
            "head": _gateway_event_cursor(),
        }

    def gateway_events_stream(client, payload):
        # Server-Sent Events surface for the dashboard.
        #
        # ``EventSource`` opens a long-lived GET, the server writes a
        # ``data: {json}\n\n`` line per ring-buffer event, and a
        # ``: keepalive\n\n`` comment every poll cycle so proxies don't
        # idle-time the connection. The dashboard mounts an
        # ``EventSource('/api/proxy/gateway/events/stream?since=…')`` and
        # falls back to the polling endpoint when SSE is unavailable.
        #
        # Implementation note: we still use the same in-process ring
        # buffer (`_GATEWAY_EVENTS`) so a single shared queue serves
        # both SSE clients and the polling clients without duplicating
        # state.
        from .local_server import StreamingResponse  # avoid import cycle on import-time

        body = payload if isinstance(payload, dict) else {}
        try:
            since = int(body.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        channel_filter = body.get("channel") if isinstance(body.get("channel"), str) else None
        platform_filter = body.get("platform") if isinstance(body.get("platform"), str) else None

        # Hard cap so a forgotten browser tab cannot hold the connection
        # forever. EventSource auto-reconnects on close so the dashboard
        # transparently picks up where it left off (it sends the last
        # ``id: …`` back as Last-Event-ID, which we honour below).
        deadline = time.time() + 30 * 60
        idle_poll_s = 1.0
        keepalive_s = 15.0

        def gen():
            cursor = since
            last_keepalive = time.time()
            # Replay the buffered tail once on connect — the dashboard
            # passes ``since=cursor`` from its previous session via the
            # Last-Event-ID header (see ``EventSource`` rfc), but on a
            # cold connect we want at least the most recent events so
            # the panel doesn't look empty.
            initial = _gateway_events_snapshot(
                since=cursor,
                channel=channel_filter,
                platform=platform_filter,
                limit=100,
            )
            for ev in initial:
                seq = int(ev.get("seq") or 0)
                if seq > cursor:
                    cursor = seq
                yield _format_sse(ev)
            yield b": ready\n\n"

            while time.time() < deadline:
                events = _gateway_events_snapshot(
                    since=cursor,
                    channel=channel_filter,
                    platform=platform_filter,
                    limit=100,
                )
                if events:
                    for ev in events:
                        seq = int(ev.get("seq") or 0)
                        if seq > cursor:
                            cursor = seq
                        yield _format_sse(ev)
                    last_keepalive = time.time()
                else:
                    # Comment line, keeps the connection warm without
                    # waking the EventSource ``onmessage`` listener.
                    if (time.time() - last_keepalive) >= keepalive_s:
                        yield b": keepalive\n\n"
                        last_keepalive = time.time()
                time.sleep(idle_poll_s)

        return StreamingResponse(generator=gen())

    return [
        ("GET", "/gateway/platforms", platforms),
        ("GET", "/gateway/config", gateway_config),
        ("POST", "/gateway/config/upsert", gateway_config_save),
        ("POST", "/gateway/config/delete", gateway_config_remove),
        ("POST", "/gateway/config/test", gateway_config_probe),
        ("GET", "/gateway/status", gateway_status),
        ("GET", "/gateway/commands", gateway_commands),
        ("POST", "/gateway/commands", gateway_commands),
        ("POST", "/gateway/inbound", gateway_inbound),
        ("POST", "/gateway/send", gateway_send),
        ("GET", "/gateway/events", gateway_events),
        ("GET", "/gateway/events/stream", gateway_events_stream),
        ("POST", "/gateway/telegram/setup", telegram_setup),
        ("POST", "/gateway/telegram/poll", telegram_poll),
        ("POST", "/gateway/telegram/send", telegram_send),
        ("POST", "/gateway/telegram/diagnose", telegram_diagnose),
    ]
