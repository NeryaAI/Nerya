from __future__ import annotations

from typing import Any
import os
import threading
import time

from ..agent.kernel import AgentKernel
from ..core import yaml_io
from ..core.atomic_write import atomic_write_text
from ..core.time import now_iso
from ..messaging import generic_platform, telegram
from ..messaging.pipeline import MessagePipeline
from ..messaging.platforms import get_platform, list_platforms, require_platform
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
from .gateway_identity import message_id as gateway_message_id, session_id as gateway_session_id
from .routes_agent import agent_reply_text


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


def sync_configured_gateways_on_start(client) -> dict[str, Any]:
    """Synchronize gateway platform runtime affordances for configured channels.

    Telegram menus are intentionally registered by Nerya itself on startup,
    not by an operator script. The command source is the shared gateway command
    registry so `/help`, `/menu`, and Bot API menus stay in lockstep.
    """
    results: list[dict[str, Any]] = []
    for channel, cfg in _configured_telegram_channels(client):
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
    configured = _configured_telegram_channels(client)
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
    return {
        "scheduled": True,
        "telegram_pollers": poller_started,
        "channels": [name for name, _ in configured],
    }


# --------------------------------------------------------- telegram polling
_TELEGRAM_POLLER_THREADS: dict[str, threading.Thread] = {}
_TELEGRAM_POLLER_STOPS: dict[str, threading.Event] = {}


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
    for upd in updates.get("updates") or []:
        update_id = upd.get("update_id")
        if isinstance(update_id, int):
            next_offset = max(int(next_offset or 0), update_id + 1)
        # Apr-29 2026 — operators can now approve / reject right from
        # the bound Telegram chat. We catch ``callback_query`` updates
        # (button presses on inline keyboards produced by
        # ``approval_prompts.build_prompt``) before falling through to
        # the regular text handler. The callback itself is forwarded
        # into the local ``/approvals/callback`` machinery so the
        # dashboard, the gateway, and the trade engine all see the
        # same source-of-truth resolution.
        cb = upd.get("callback_query") or {}
        if cb:
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
        text = msg.get("text") or ""
        if not chat_id or not text:
            continue
        if configured_chat and chat_id != configured_chat:
            processed.append({"ok": False, "chat_id": chat_id,
                              "error": "chat_not_allowed"})
            continue
        try:
            processed.append(
                _handle_text(client, cfg, chat_id, text, update_id)
            )
        except Exception as exc:
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
    configured_count = 0
    poller_count = 0
    for name, raw_cfg in sorted(channels.items()):
        cfg = dict(raw_cfg or {})
        kind = str(cfg.get("kind") or ("telegram" if name == "telegram" else "")).lower()
        if kind != "telegram":
            continue
        token_ref = cfg.get("bot_token_ref") or cfg.get("token_ref")
        configured = bool(token_ref)
        if configured:
            configured_count += 1
        polling_enabled = configured and _channel_uses_polling(cfg)
        thread = _TELEGRAM_POLLER_THREADS.get(str(name))
        poller_alive = bool(thread is not None and thread.is_alive())
        if poller_alive:
            poller_count += 1
        mode = str(cfg.get("mode") or ("polling" if polling_enabled else "webhook"))
        telegram_channels.append({
            "channel": str(name),
            "kind": "telegram",
            "configured": configured,
            "bot_token_ref_configured": bool(token_ref),
            "chat_id_configured": bool(cfg.get("chat_id")),
            "polling_enabled": polling_enabled,
            "poller_alive": poller_alive,
            "mode": mode,
        })
    return {
        "ok": True,
        "channels_file_exists": client.config.paths.messages_channels.exists(),
        "configured_gateway_count": configured_count,
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


def _reply(client, cfg: dict[str, Any], chat_id: str, text: str) -> dict[str, Any]:
    msg = {
        "message_id": gateway_message_id("telegram", chat_id=chat_id, direction="reply"),
        "channel": "telegram",
        "kind": "telegram",
        "text": text,
        "ts": now_iso(),
        "delivered": False,
        "rate_limited": False,
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


def _attach_telegram_progress_hooks(kernel: AgentKernel, client, cfg: dict[str, Any], chat_id: str) -> None:
    seen: set[str] = set()

    def emit(ctx) -> None:
        text = hook_status_text(ctx.phase, ctx.data, ctx.iteration)
        if not text or text in seen:
            return
        seen.add(text)
        _send_progress(client, cfg, chat_id, text)

    for phase in (
        "after_plan",
        "after_subagents",
        "before_think",
        "after_think",
        "after_act",
        "after_observe",
        "before_close",
    ):
        try:
            kernel.hooks.register(phase, emit)
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
                 update_id: int | None = None) -> dict[str, Any]:
    clean = (text or "").strip()
    state = _load_state(client)
    active_sessions = state.get("active_sessions") if isinstance(state, dict) else {}
    if not isinstance(active_sessions, dict):
        active_sessions = {}
    session_id = str(
        active_sessions.get(str(chat_id))
        or gateway_session_id("telegram", chat_id=chat_id)
    )

    if clean.startswith("/"):
        outcome = GATEWAY_COMMAND_REGISTRY.handle(
            clean,
            CommandContext(
                client=client,
                platform="telegram",
                chat_id=str(chat_id),
                session_id=session_id,
                raw_text=clean,
                state=state,
                save_state=lambda new_state: _save_state(client, dict(new_state)),
                delete_session=lambda sid: _delete_session(client, sid),
                dashboard_url=resolve_dashboard_url(client.config),
            ),
        )
        if outcome.handled:
            sent = _reply(client, cfg, chat_id, outcome.reply_text)
            return {
                "ok": True,
                "command": outcome.command,
                "reply_text": outcome.reply_text,
                "delivery": sent,
            }

    scan = scan_and_redact(clean, buffer=get_default_buffer())
    redacted = scan.redacted_text
    captured_notice = ""
    if scan.captured:
        captured_notice = _format_capture_notice(scan.captures)
        try:
            _reply(client, cfg, chat_id, captured_notice)
        except Exception:
            pass

    GatewayMirror(client.config.paths).record_inbound(
        channel="telegram",
        handle=str(chat_id),
        session_id=session_id,
        payload={
            "text": redacted,
            "update_id": update_id,
            "secrets_captured": len(scan.captures),
        },
    )
    stop_typing = threading.Event()
    typing_thread = threading.Thread(
        target=_typing_until_done, args=(client, cfg, chat_id, stop_typing), daemon=True
    )
    typing_thread.start()
    try:
        kernel = AgentKernel(config=client.config, skills=client.skills)
        _attach_telegram_progress_hooks(kernel, client, cfg, chat_id)
        result = kernel.run_turn(
            trigger={
                "source": "telegram",
                "kind": "user.chat",
                "target": "main",
                "payload": {
                    "text": redacted,
                    "channel": "telegram",
                    "chat_id": str(chat_id),
                    "secret_tokens": [c.token for c in scan.captures],
                    "secret_kinds": [c.kind for c in scan.captures],
                },
            },
            session_id=session_id,
        )
    finally:
        stop_typing.set()
    trace_text = compact_turn_summary(result)
    if trace_text:
        _reply(client, cfg, chat_id, trace_text)
    reply = agent_reply_text(result)
    sent = _reply(client, cfg, chat_id, reply)
    state = _load_state(client)
    state["last_turn_id"] = result.turn_id
    state["last_trace"] = trace_text
    _save_state(client, state)
    GatewayMirror(client.config.paths).record_outbound(
        channel="telegram",
        handle=str(chat_id),
        session_id=session_id,
        payload={"text": reply, "delivery": sent},
    )
    return {
        "ok": True,
        "turn_id": result.turn_id,
        "reply_text": reply,
        "delivery": sent,
        "events": turn_events(result),
        "trace_text": trace_text,
        "secrets_captured": [c.asdict() for c in scan.captures],
        "captured_notice": captured_notice,
    }


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


def _emit_status(client, cfg: dict[str, Any], text: str) -> None:
    try:
        kind = str(cfg.get("kind") or "")
        if kind == "telegram" and cfg.get("chat_id"):
            _typing(client, cfg, str(cfg.get("chat_id")))
            return
        generic_platform.send_status(
            channel_cfg=cfg,
            text=text,
            resolve_secret=_secret_resolver(client),
        )
    except Exception:
        pass


def _run_gateway_turn(client, *, platform: str, chat_id: str, text: str,
                      session_id: str | None = None,
                      progress_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    state = _load_state(client)
    active_key = f"{platform}:{chat_id}"
    active_sessions = state.get("active_sessions") if isinstance(state, dict) else {}
    if not isinstance(active_sessions, dict):
        active_sessions = {}
    session = (
        session_id
        or active_sessions.get(active_key)
        or active_sessions.get(str(chat_id))
        or gateway_session_id(platform, chat_id=chat_id)
    )
    session = str(session)
    cfg = progress_cfg or {"kind": platform}

    if text.strip().startswith("/"):
        outcome = GATEWAY_COMMAND_REGISTRY.handle(
            text,
            CommandContext(
                client=client,
                platform=platform,
                chat_id=str(chat_id),
                session_id=session,
                raw_text=text,
                state=state,
                save_state=lambda new_state: _save_state(client, dict(new_state)),
                delete_session=lambda sid: _delete_session(client, sid),
                dashboard_url=resolve_dashboard_url(client.config),
            ),
        )
        if outcome.handled:
            return {
                "ok": True,
                "platform": platform,
                "chat_id": str(chat_id),
                "session_id": session,
                "command": outcome.command,
                "reply_text": outcome.reply_text,
            }

    kernel = AgentKernel(config=client.config, skills=client.skills)

    def emit(ctx) -> None:
        status = hook_status_text(ctx.phase, ctx.data, ctx.iteration)
        if status:
            _emit_status(client, cfg, status)

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
    if scan.captured:
        notice = _format_capture_notice(scan.captures)
        if notice:
            try:
                _emit_status(client, cfg, notice)
            except Exception:
                pass

    GatewayMirror(client.config.paths).record_inbound(
        channel=platform,
        handle=str(chat_id),
        session_id=session,
        payload={"text": redacted, "secrets_captured": len(scan.captures)},
    )
    result = kernel.run_turn(
        trigger={
            "source": platform,
            "kind": "user.chat",
            "target": "main",
            "payload": {
                "text": redacted,
                "channel": platform,
                "chat_id": str(chat_id),
                "secret_tokens": [c.token for c in scan.captures],
                "secret_kinds": [c.kind for c in scan.captures],
            },
        },
        session_id=session,
    )
    reply = agent_reply_text(result)
    events = turn_events(result)
    trace_text = compact_turn_summary(result)
    GatewayMirror(client.config.paths).record_outbound(
        channel=platform,
        handle=str(chat_id),
        session_id=session,
        payload={"text": reply, "events": events},
    )
    return {
        "ok": True,
        "platform": platform,
        "chat_id": str(chat_id),
        "session_id": session,
        "turn_id": result.turn_id,
        "reply_text": reply,
        "trace_text": trace_text,
        "events": events,
    }


def routes():
    def platforms(client, _payload):
        return {"platforms": list_platforms()}

    def gateway_status(client, _payload):
        return gateway_runtime_status(client)

    def gateway_inbound(client, payload):
        platform = str(payload.get("platform") or payload.get("kind") or "webhook").lower()
        spec = require_platform(platform)
        chat_id = str(payload.get("chat_id") or payload.get("conversation_id") or payload.get("user_id") or "default")
        text = str(payload.get("text") or payload.get("message") or "")
        if not text.strip():
            return {"ok": False, "error": "text required", "platform": spec.id}
        channel = str(payload.get("channel") or spec.id)
        cfg = _channel_cfg(client, channel, spec.id)
        if chat_id and not cfg.get("chat_id"):
            cfg["chat_id"] = chat_id
        return _run_gateway_turn(
            client, platform=spec.id, chat_id=chat_id, text=text,
            session_id=payload.get("session_id"), progress_cfg=cfg,
        )

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

    return [
        ("GET", "/gateway/platforms", platforms),
        ("GET", "/gateway/status", gateway_status),
        ("POST", "/gateway/inbound", gateway_inbound),
        ("POST", "/gateway/send", gateway_send),
        ("POST", "/gateway/telegram/setup", telegram_setup),
        ("POST", "/gateway/telegram/poll", telegram_poll),
        ("POST", "/gateway/telegram/send", telegram_send),
    ]
