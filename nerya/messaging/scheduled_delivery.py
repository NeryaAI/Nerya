"""Scheduled-session delivery fan-out (Hermes parity, Phase C).

After a scheduled agent session finishes, its result is routed to any
``delivery_targets`` declared on the :class:`ScheduleEntry`. Two target
kinds are supported:

* ``messages`` — hand the turn's textual summary to the regular
  :class:`MessagePipeline` on the configured channel. This reuses every
  guardrail the message path already enforces (rate limits, template
  rendering, vault-backed credentials, outbox journaling).
* ``webhook`` — POST a compact JSON envelope to the configured URL.
  Optional per-target headers are honoured verbatim; no secret material
  is inferred.

Each target produces a structured delivery record that is both embedded
into the scheduled-session journal row *and* returned to the caller so
:class:`ScheduledSessionRunner` can expose it on
:class:`ScheduledSessionResult`.

This module lives under ``nerya/messaging/`` (not ``nerya/triggers/``)
because delivery is a messaging-side concern: we render text, call the
:class:`MessagePipeline`, journal an outbox row. The runtime-ownership
ADR forbids ``triggers`` from importing ``messaging``, so the wiring
flows the other way — :class:`CronScheduler` injects this function into
:class:`ScheduledSessionRunner` at construction time.
"""

from __future__ import annotations

import json
from typing import Any

from ..core import jsonl
from ..core.config import Config
from ..core.time import now_iso
from .pipeline import MessagePipeline
from .transport import MessagingTransport, UrllibMessagingTransport

# NOTE: ``ScheduleEntry`` (defined in ``nerya.triggers.schedule``) is the
# concrete shape callers pass in here, but we deliberately keep the
# annotations as plain ``Any`` instead of importing the dataclass — even
# under ``TYPE_CHECKING``. The runtime-ownership ADR forbids
# ``messaging`` from importing ``triggers``, and
# ``tests/test_architecture_audit.py`` AST-walks every import node so a
# guarded import would still be flagged. Treat ``entry`` here as the
# duck-typed scheduled-session payload it actually is at runtime.


# --------------------------------------------------------------- helpers


def _render_summary(result: Any, entry: Any) -> str:
    """Collapse an :class:`AgentTurnResult`-shaped object into one line.

    The result might not always have the exact shape (tests can pass in
    a dict). We probe defensively so delivery never blows up on a
    partial turn result.
    """

    if isinstance(result, dict):
        decision = result.get("decision")
        actions = result.get("actions") or []
        stopped_reason = result.get("stopped_reason")
    else:
        decision = getattr(result, "decision", None)
        actions = getattr(result, "actions", None) or []
        stopped_reason = getattr(result, "stopped_reason", None)

    msg: str | None = None
    if isinstance(decision, dict):
        for key in ("message", "summary", "text", "headline", "note"):
            v = decision.get(key)
            if isinstance(v, str) and v.strip():
                msg = v.strip()
                break
    if msg is None and actions:
        first = actions[0] if isinstance(actions[0], dict) else {}
        skill = first.get("skill") or first.get("skill_id") or "?"
        action = first.get("action") or "?"
        msg = f"scheduled session ran {skill}.{action}"
    if msg is None:
        if stopped_reason:
            msg = f"scheduled session stopped: {stopped_reason}"
        else:
            msg = f"scheduled session {entry.id} produced no decision"
    return msg


def _result_envelope(result: Any, entry: Any) -> dict[str, Any]:
    """Compact JSON envelope shared across webhook + messages targets."""

    if isinstance(result, dict):
        turn_id = result.get("turn_id")
        trigger_event_id = result.get("trigger_event_id")
        decision = result.get("decision")
        actions = list(result.get("actions") or [])
        stopped_reason = result.get("stopped_reason")
    else:
        turn_id = getattr(result, "turn_id", None)
        trigger_event_id = getattr(result, "trigger_event_id", None)
        decision = getattr(result, "decision", None)
        actions = list(getattr(result, "actions", None) or [])
        stopped_reason = getattr(result, "stopped_reason", None)

    return {
        "schedule_id": entry.id,
        "target": entry.target,
        "strategy_id": entry.strategy_id,
        "turn_id": turn_id,
        "trigger_event_id": trigger_event_id,
        "decision": decision,
        "actions": actions,
        "stopped_reason": stopped_reason,
        "session_kind": entry.session_kind,
        "ts": now_iso(),
    }


# --------------------------------------------------------------- targets


def _deliver_messages(config: Config, entry: Any,
                      target: dict[str, Any], result: Any,
                      pipeline: MessagePipeline) -> dict[str, Any]:
    channel = str(target.get("channel") or "").strip()
    if not channel:
        return {
            "ok": False,
            "kind": "messages",
            "error": "messages target missing 'channel'",
        }
    text = _render_summary(result, entry)
    try:
        out = pipeline.send(
            channel=channel,
            text=text,
            strategy_id=entry.strategy_id,
            context={
                "schedule_id": entry.id,
                "session_kind": entry.session_kind,
            },
        )
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "ok": False,
            "kind": "messages",
            "channel": channel,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": bool(out.get("delivered")),
        "kind": "messages",
        "channel": channel,
        "message_id": out.get("message_id"),
        "rate_limited": bool(out.get("rate_limited")),
        "delivery_note": out.get("delivery_note"),
    }


def _deliver_webhook(config: Config, entry: Any,
                     target: dict[str, Any], result: Any,
                     transport: MessagingTransport) -> dict[str, Any]:
    url = str(target.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {
            "ok": False,
            "kind": "webhook",
            "error": "webhook target missing http(s) url",
        }
    headers = {"Content-Type": "application/json"}
    for k, v in (target.get("headers") or {}).items():
        headers[str(k)] = str(v)
    body = _result_envelope(result, entry)
    try:
        status, resp = transport.post(url, headers=headers, body=body,
                                      timeout=10.0)
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "ok": False,
            "kind": "webhook",
            "url": url,
            "error": f"{type(exc).__name__}: {exc}",
        }
    ok = 200 <= int(status) < 300
    resp_snippet: Any = resp
    try:
        raw = json.dumps(resp)[:400]
        resp_snippet = raw
    except Exception:
        resp_snippet = repr(resp)[:400]
    return {
        "ok": ok,
        "kind": "webhook",
        "url": url,
        "status": int(status),
        "response": resp_snippet,
    }


# ------------------------------------------------------------ entry point


def deliver_scheduled_session(config: Config, entry: Any,
                              result: Any,
                              *,
                              pipeline: MessagePipeline | None = None,
                              transport: MessagingTransport | None = None,
                              ) -> list[dict[str, Any]]:
    """Fan ``result`` out to every configured delivery target.

    Returns a list with one structured dict per target (order preserved)
    so :class:`ScheduledSessionRunner` can surface it to operators. A
    dedicated ``journals/scheduled_session_delivery.jsonl`` row is also
    appended per target for long-term audit.
    """

    out: list[dict[str, Any]] = []
    pipeline = pipeline or MessagePipeline(config=config)
    transport = transport or UrllibMessagingTransport()

    for target in entry.delivery_targets or []:
        kind = str(target.get("kind") or "").strip().lower()
        if kind == "messages":
            row = _deliver_messages(config, entry, target, result, pipeline)
        elif kind == "webhook":
            row = _deliver_webhook(config, entry, target, result, transport)
        else:
            row = {
                "ok": False,
                "kind": kind or "unknown",
                "error": f"unknown delivery kind: {kind!r}",
            }
        out.append(row)
        try:
            jsonl.append(
                config.paths.journal("scheduled_session_delivery"),
                {
                    "kind": "scheduled_session.delivery",
                    "ts": now_iso(),
                    "schedule_id": entry.id,
                    "target_kind": row.get("kind"),
                    "ok": row.get("ok"),
                    "detail": row,
                },
            )
        except Exception:  # pragma: no cover - best-effort journaling
            pass

    return out


__all__ = ["deliver_scheduled_session"]
