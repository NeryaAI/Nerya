"""Plan 21 P0 §2 — inline approval prompts for messaging platforms.

Hermes ships approve / reject buttons that are tied to an approval id
and the actor that initiated the action. Nerya now produces the same
shape from the existing ``ApprovalRecord`` / pending-approval JSONL
rows: a platform-agnostic :class:`ApprovalPrompt` plus per-platform
renderers that operators can plug into their gateway pipeline.

Today we ship a Telegram inline-keyboard renderer; Discord, Slack,
Feishu and Generic are scaffolded so the gateway pipeline can dispatch
on platform name without inserting `if "telegram":` checks elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


# Inbound callback_data shape.  ``approve:`` / ``reject:`` prefixes are
# parsed by :func:`parse_callback_data` so the gateway can route the
# button press straight into ``ApprovalGate.approve`` /
# ``ApprovalGate.reject``.
CALLBACK_PREFIX_APPROVE = "approve"
CALLBACK_PREFIX_REJECT = "reject"
CALLBACK_PREFIX_DETAILS = "details"


@dataclass
class ApprovalButton:
    label: str
    callback_data: str
    style: str = "default"  # "primary" | "danger" | "default"

    def as_telegram(self) -> dict[str, Any]:
        return {"text": self.label, "callback_data": self.callback_data}

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "callback_data": self.callback_data,
            "style": self.style,
        }


@dataclass
class ApprovalPrompt:
    approval_id: str
    actor_id: str
    text: str
    buttons: list[ApprovalButton] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "actor_id": self.actor_id,
            "text": self.text,
            "buttons": [b.as_dict() for b in self.buttons],
            "metadata": dict(self.metadata),
        }

    def telegram_reply_markup(self, *, columns: int = 2) -> dict[str, Any]:
        rows: list[list[dict[str, Any]]] = []
        bucket: list[dict[str, Any]] = []
        for b in self.buttons:
            bucket.append(b.as_telegram())
            if len(bucket) >= max(1, columns):
                rows.append(bucket)
                bucket = []
        if bucket:
            rows.append(bucket)
        return {"inline_keyboard": rows}


def _format_intent(intent: dict[str, Any]) -> str:
    parts: list[str] = []
    market = intent.get("market") or intent.get("symbol") or ""
    side = intent.get("side") or ""
    size = intent.get("size") or intent.get("notional_usd") or ""
    order_type = intent.get("order_type") or "market"
    if market or side:
        parts.append(f"{side.upper()} {market}".strip())
    if size:
        parts.append(f"{size}")
    if order_type:
        parts.append(f"{order_type}")
    return " · ".join(p for p in parts if p)


def _format_risk(risk: dict[str, Any]) -> str:
    reasons = risk.get("reasons") or []
    if not isinstance(reasons, list):
        return ""
    out = "; ".join(str(r) for r in reasons[:3])
    return out


def _tool_from_item(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    tool = item.get("tool") if isinstance(item.get("tool"), dict) else {}
    payload_tool = payload.get("tool") if isinstance(payload.get("tool"), dict) else {}
    return {**payload_tool, **tool}


def _arguments_from_item(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    args = payload.get("arguments") or item.get("arguments") or {}
    return args if isinstance(args, dict) else {}


def _items_from_record(record: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = record.get("items") or payload.get("items") or []
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def _summarize_tool_arguments(args: dict[str, Any]) -> str:
    for key in ("cmd", "command", "path", "file", "url", "market", "symbol"):
        value = args.get(key)
        if value is not None and str(value).strip():
            return f"{key}={str(value).strip()[:120]}"
    parts: list[str] = []
    for key, value in list(args.items())[:4]:
        text = str(value)
        if len(text) > 80:
            text = text[:77] + "..."
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def build_prompt(
    record: dict[str, Any],
    *,
    actor_id: str = "",
    extra_buttons: list[ApprovalButton] | None = None,
) -> ApprovalPrompt:
    """Build a platform-agnostic approval prompt from an approval row.

    ``record`` accepts both the SQL row shape (``id``, ``payload``) and
    the JSONL pending-approval shape produced by
    :class:`ApprovalGate.require`.
    """

    aid = (
        record.get("approval_id")
        or record.get("id")
        or ""
    )
    payload = record.get("payload") or {}
    kind = str(record.get("kind") or payload.get("kind") or "approval")
    intent = payload.get("intent") or record.get("intent") or {}
    risk = payload.get("risk") or record.get("risk") or {}

    # Compose the human-readable summary.
    lines: list[str] = []
    if kind == "tool_permission_batch":
        clean_items = _items_from_record(record, payload)
        lines.append(f"Permission batch - {aid}")
        lines.append(f"{len(clean_items) or 1} tool call(s) require approval")
        for idx, item in enumerate(clean_items[:8], start=1):
            tool = _tool_from_item(item)
            tool_name = tool.get("name") or item.get("action") or "tool"
            skill_id = tool.get("skill_id") or item.get("skill_id") or "native"
            reason = str(item.get("reason") or "").strip()
            arg_summary = _summarize_tool_arguments(_arguments_from_item(item))
            detail = arg_summary or reason
            lines.append(
                f"{idx}. {skill_id}.{tool_name}"
                + (f" - {detail}" if detail else "")
            )
        if len(clean_items) > 8:
            lines.append(f"... plus {len(clean_items) - 8} more")
    elif kind == "tool_permission":
        tool = payload.get("tool") or record.get("tool") or {}
        tool_name = (
            tool.get("name")
            or record.get("action")
            or record.get("tool")
            or "tool"
        )
        skill_id = tool.get("skill_id") or record.get("skill_id") or "native"
        lines.append(f"Permission requested · {aid}")
        lines.append(f"Tool: {skill_id}.{tool_name}")
        reason = record.get("reason") or payload.get("reason") or ""
        if reason:
            lines.append(f"Reason: {reason}")
    else:
        intent_summary = _format_intent(intent) or record.get("kind", "approval")
        lines.append(f"Approval requested · {aid}")
        lines.append(f"Intent: {intent_summary}")
    risk_summary = _format_risk(risk)
    if risk_summary:
        lines.append(f"Risk: {risk_summary}")
    if record.get("strategy_id"):
        lines.append(f"Strategy: {record.get('strategy_id')}")
    if actor_id:
        lines.append(f"Actor: {actor_id}")
    text = "\n".join(lines)

    buttons = [
        ApprovalButton(
            label="✅ Approve",
            callback_data=f"{CALLBACK_PREFIX_APPROVE}:{aid}",
            style="primary",
        ),
        ApprovalButton(
            label="❌ Reject",
            callback_data=f"{CALLBACK_PREFIX_REJECT}:{aid}",
            style="danger",
        ),
        ApprovalButton(
            label="🔍 Details",
            callback_data=f"{CALLBACK_PREFIX_DETAILS}:{aid}",
            style="default",
        ),
    ]
    if extra_buttons:
        buttons.extend(extra_buttons)

    batch_items = _items_from_record(record, payload)
    return ApprovalPrompt(
        approval_id=str(aid),
        actor_id=str(actor_id or ""),
        text=text,
        buttons=buttons,
        metadata={
            "kind": kind or "trade_intent",
            "intent": intent,
            "risk": risk,
            "tool": payload.get("tool") or record.get("tool") or {},
            "items": batch_items,
            "tool_batch": kind == "tool_permission_batch",
            "tool_count": (
                len(batch_items)
                if kind == "tool_permission_batch"
                else 1 if kind == "tool_permission" else 0
            ),
            "strategy_id": record.get("strategy_id") or intent.get("strategy_id"),
            "market": record.get("market") or intent.get("market"),
        },
    )


def parse_callback_data(callback_data: str) -> tuple[str, str]:
    """Parse a callback_data string into ``(action, approval_id)``.

    Returns ``("", "")`` when the payload does not match a known prefix
    so callers can ignore unrelated callbacks safely.
    """

    if not callback_data or ":" not in callback_data:
        return "", ""
    head, _, tail = callback_data.partition(":")
    head = head.strip().lower()
    aid = tail.strip()
    if head not in {
        CALLBACK_PREFIX_APPROVE,
        CALLBACK_PREFIX_REJECT,
        CALLBACK_PREFIX_DETAILS,
    }:
        return "", ""
    return head, aid


@dataclass
class CallbackResolution:
    action: str
    approval_id: str
    state: str  # "approved" | "rejected" | "details" | "ignored" | "error"
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "approval_id": self.approval_id,
            "state": self.state,
            "note": self.note,
        }


def resolve_callback(
    callback_data: str,
    *,
    actor_id: str,
    approve: Callable[[str], None] | None = None,
    reject: Callable[[str, str], None] | None = None,
    actor_owns: Callable[[str, str], bool] | None = None,
) -> CallbackResolution:
    """Apply an inbound platform callback (button press).

    Plan 21 P0 §2 — Hermes parity: only the actor that owns the
    approval id may resolve it. Operators can plug a custom
    ``actor_owns`` predicate to enforce that. When ``actor_owns`` is
    ``None`` we accept the callback as-is so the existing single-tenant
    Nerya install is back-compat.
    """

    action, aid = parse_callback_data(callback_data)
    if not action or not aid:
        return CallbackResolution(action="", approval_id="", state="ignored",
                                  note="callback_data did not match approve/reject/details")
    if actor_owns is not None and not actor_owns(actor_id, aid):
        return CallbackResolution(action=action, approval_id=aid,
                                  state="error",
                                  note="actor does not own this approval")
    try:
        if action == CALLBACK_PREFIX_APPROVE:
            if approve is None:
                return CallbackResolution(action, aid, "error",
                                          "approve callback not wired")
            approve(aid)
            return CallbackResolution(action, aid, "approved")
        if action == CALLBACK_PREFIX_REJECT:
            if reject is None:
                return CallbackResolution(action, aid, "error",
                                          "reject callback not wired")
            reject(aid, f"rejected via {actor_id or 'gateway'} button")
            return CallbackResolution(action, aid, "rejected")
        if action == CALLBACK_PREFIX_DETAILS:
            return CallbackResolution(action, aid, "details")
    except Exception as exc:
        return CallbackResolution(action, aid, "error", str(exc))
    return CallbackResolution(action, aid, "ignored")
