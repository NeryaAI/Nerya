"""Gateway-facing progress and command helpers.

This module turns the kernel's auditable turn steps into short user-visible
messages. It deliberately contains no secrets and no transport code; routes can
use the same summaries for dashboard, Telegram, Discord, or future gateways.

Command + menu rendering is delegated to :mod:`nerya.api.gateway_commands`
(single gateway registry shared by every adapter).
``DEFAULT_COMMANDS`` and ``command_help_text`` are kept as thin shims for
backwards compatibility with code that imported them directly.
"""

from __future__ import annotations

from typing import Any

from .gateway_commands import help_text as _registry_help_text, menu_commands as _registry_menu


# Backwards-compatible alias kept for callers still importing the list. The
# registry is the source of truth; mutating this list will not change the
# rendered menu.
DEFAULT_COMMANDS: list[dict[str, str]] = _registry_menu()


def command_help_text(*, platform: str | None = None) -> str:
    return _registry_help_text(platform=platform)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _detail(step: dict[str, Any]) -> dict[str, Any]:
    detail = step.get("detail")
    return detail if isinstance(detail, dict) else {}


def _join_names(values: Any, *, empty: str = "none") -> str:
    items = [str(v) for v in _as_list(values) if str(v)]
    if not items:
        return empty
    text = ", ".join(items[:4])
    if len(items) > 4:
        text += f" +{len(items) - 4}"
    return text


def _truncate(text: str, limit: int = 240) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def step_event(step: dict[str, Any]) -> dict[str, Any] | None:
    """Render one transcript step into a UI-friendly event row.

    The workspace-native loop emits
    :class:`~nerya.agent.transcript_blocks.BlockEnvelope` dicts whose
    ``block.kind`` is one of ``text`` / ``thinking`` / ``tool_use`` /
    ``tool_result``. We translate those onto the same ``phase / status
    / text`` shape the dashboard already understands.
    """

    block = step.get("block") if isinstance(step.get("block"), dict) else step
    kind = str(block.get("kind") or "")
    role = str(step.get("role") or block.get("role") or "")
    elapsed_ms = block.get("elapsed_ms")
    suffix = (
        f" · {int(elapsed_ms)}ms"
        if isinstance(elapsed_ms, (int, float)) and elapsed_ms
        else ""
    )

    if kind == "text":
        body = _truncate(str(block.get("text") or ""))
        if not body:
            return None
        return {
            "phase": "message",
            "status": "ok",
            "text": f"💬 {body}",
            "wall_ms": None,
            "detail": {"role": role or "assistant"},
        }
    if kind == "thinking":
        body = _truncate(str(block.get("text") or block.get("summary") or ""))
        return {
            "phase": "thinking",
            "status": "ok",
            "text": f"🧠 {body}" if body else "🧠 thinking…",
            "wall_ms": None,
            "detail": {
                "summary": str(block.get("summary") or ""),
            },
        }
    if kind == "tool_use":
        action = str(block.get("action") or "")
        skill_id = str(block.get("skill_id") or "native")
        return {
            "phase": "tool_use",
            "status": "pending",
            "text": f"⚙️ {skill_id}.{action} · invoking",
            "wall_ms": None,
            "detail": {
                "skill_id": skill_id,
                "action": action,
                "call_id": block.get("call_id"),
                "payload": block.get("payload") or {},
            },
        }
    if kind == "tool_result":
        ok = bool(block.get("ok"))
        action = str(block.get("action") or "")
        skill_id = str(block.get("skill_id") or "native")
        emoji = "⚙️" if ok else "⚠️"
        status = "ok" if ok else "error"
        err = block.get("error_kind") or block.get("error") or ""
        text = f"{emoji} {skill_id}.{action} · {status}"
        if not ok and err:
            text += f" · {err}"
        return {
            "phase": "tool_result",
            "status": status,
            "text": text + suffix,
            "wall_ms": int(elapsed_ms) if isinstance(elapsed_ms, (int, float)) else None,
            "detail": {
                "skill_id": skill_id,
                "action": action,
                "call_id": block.get("call_id"),
                "ok": ok,
                "error": block.get("error"),
                "error_kind": block.get("error_kind"),
            },
        }
    return None


def turn_events(result: Any) -> list[dict[str, Any]]:
    """Project a turn result onto a flat list of UI-friendly events.

    Reads ``result.blocks`` first (the workspace-native canonical
    transcript). Falls back to ``result.steps`` for callers passing a
    legacy result snapshot — both fields carry the same shape under the
    consolidated kernel.
    """

    raw = getattr(result, "blocks", None) or getattr(result, "steps", None) or []
    events: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            event = step_event(entry)
            if event is not None:
                events.append(event)
    return events


def compact_turn_summary(result: Any, *, max_events: int = 8) -> str:
    events = turn_events(result)
    if not events:
        return ""
    shown = events[:max_events]
    lines = ["Agent trace:"]
    lines.extend(f"{i + 1}. {event['text']}" for i, event in enumerate(shown))
    if len(events) > len(shown):
        lines.append(f"… {len(events) - len(shown)} more step(s) in dashboard trace")
    return "\n".join(lines)


def hook_status_text(phase: str, data: dict[str, Any] | None = None, iteration: int = 0) -> str | None:
    data = data or {}
    if phase == "after_plan":
        return (
            "🧭 Planning done: "
            f"route={data.get('plan_kind') or 'unknown'}, "
            f"tier={data.get('plan_tier') or 'default'}, "
            f"skills={_join_names(data.get('planned_skills'))}, "
            f"subagents={_join_names(data.get('planned_subagents'))}"
        )
    if phase == "after_subagents" and data.get("planned"):
        return (
            "🧩 Subagents returned: "
            f"outputs={data.get('outputs_count', 0)}, errors={len(_as_list(data.get('errors')))}"
        )
    if phase == "before_think":
        return f"🧠 Thinking with {data.get('tier') or 'selected'} model · iteration={iteration}"
    if phase == "after_think":
        if data.get("ok") is False:
            return f"⚠️ Model decision failed: {data.get('error') or 'unknown error'}"
        return f"🧠 Decision parsed: action={data.get('decision_action') or 'noop'}"
    if phase == "after_act":
        emoji = "⚙️" if data.get("ok") else "⚠️"
        status = "ok" if data.get("ok") else data.get("error_kind") or "error"
        return f"{emoji} Tool finished: {data.get('skill')}.{data.get('action')} · {status}"
    if phase == "after_observe":
        return f"👁 Observed {data.get('observations_count', 0)} result(s); checking whether to continue"
    if phase == "before_close":
        return f"✅ Closing turn: top_action={data.get('top_action') or 'noop'}, actions={data.get('actions_count', 0)}"
    return None
