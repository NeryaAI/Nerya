"""Trade execution notification fan-out.

This module keeps trade notifications on the existing message pipeline:
channels still come from ``messages/channels.yml``, secrets still resolve
through the vault, and every delivery writes the usual message journal row.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..core import jsonl, yaml_io
from ..core.config import Config
from ..core.time import now_iso
from .pipeline import MessagePipeline


_TRADE_TOPICS = {
    "all",
    "trade",
    "trades",
    "trading",
    "order",
    "orders",
    "fill",
    "fills",
    "execution",
    "executions",
}
_LOCAL_KINDS = {"dashboard", "local"}


def event_from_order_result(
    *,
    intent: Any,
    result: Any,
    session_id: str,
    risk_decision: dict[str, Any] | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    order = _asdict(result)
    return {
        "kind": "trade.execution",
        "status": str(order.get("status") or ""),
        "strategy_id": _attr(intent, "strategy_id"),
        "account_id": _attr(intent, "account_id"),
        "market": _attr(intent, "market"),
        "side": _attr(intent, "side"),
        "source": _attr(intent, "source"),
        "intent_id": _attr(intent, "intent_id") or order.get("intent_id"),
        "order_id": order.get("order_id"),
        "session_id": session_id,
        "avg_price": order.get("avg_price"),
        "filled_size": order.get("filled_size"),
        "notional_usd": order.get("notional_usd"),
        "fee_usd": order.get("fee_usd"),
        "fills": list(order.get("fills") or []),
        "risk_decision": risk_decision,
        "approval": approval,
        "ts": now_iso(),
    }


def event_from_executor_run(
    *,
    intent: Any,
    plan: Any,
    run: Any,
    session_id: str,
    status: str,
    risk_decision: dict[str, Any] | None = None,
    budget_decision: dict[str, Any] | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(getattr(run, "result_json", None) or {})
    order_ids = list(getattr(run, "order_ids", None) or [])
    return {
        "kind": "trade.execution",
        "status": str(status or getattr(run, "state", "") or ""),
        "strategy_id": _attr(intent, "strategy_id") or _attr(plan, "strategy_id"),
        "account_id": _attr(intent, "account_id") or _attr(plan, "account_id"),
        "market": _attr(intent, "market") or _attr(plan, "market"),
        "side": _attr(intent, "side") or _attr(plan, "buy_or_sell") or _attr(plan, "side"),
        "source": _attr(intent, "source") or _attr(plan, "source"),
        "intent_id": _attr(intent, "intent_id") or getattr(run, "intent_id", None),
        "plan_id": _attr(plan, "plan_id") or getattr(run, "plan_id", None),
        "executor_id": getattr(run, "executor_id", None),
        "order_id": order_ids[0] if order_ids else None,
        "order_ids": order_ids,
        "session_id": session_id,
        "avg_price": result.get("fill_price") or result.get("avg_price"),
        "filled_size": result.get("size_base") or result.get("filled_size"),
        "notional_usd": result.get("notional_usd"),
        "fee_usd": result.get("fee_usd"),
        "executor_state": getattr(run, "state", None),
        "close_type": getattr(run, "close_type", None),
        "risk_decision": risk_decision,
        "budget_decision": budget_decision,
        "approval": approval,
        "ts": now_iso(),
    }


def broadcast_trade_event(config: Config, event: dict[str, Any]) -> dict[str, Any]:
    """Best-effort send of a trade event to configured gateway channels."""

    if not bool(config.get("messaging.trade_notifications.enabled", True)):
        return {"ok": True, "enabled": False, "channels": [], "deliveries": []}

    channels = _trade_notification_channels(config)
    if not channels:
        return {"ok": True, "enabled": True, "channels": [], "deliveries": []}

    text = format_trade_message(event)
    pipeline = MessagePipeline(config=config)
    deliveries: list[dict[str, Any]] = []
    for channel, cfg in channels:
        kind = _channel_kind(channel, cfg)
        try:
            out = pipeline.send(
                channel=channel,
                text=text,
                strategy_id=_clean_str(event.get("strategy_id")) or None,
                context={"trade": event},
            )
            deliveries.append({
                "ok": not bool(out.get("rate_limited")),
                "channel": channel,
                "kind": kind,
                "message_id": out.get("message_id"),
                "delivered": bool(out.get("delivered")),
                "rate_limited": bool(out.get("rate_limited")),
                "delivery_note": out.get("delivery_note"),
            })
        except Exception as exc:  # pragma: no cover - defensive by design
            deliveries.append({
                "ok": False,
                "channel": channel,
                "kind": kind,
                "error": f"{type(exc).__name__}: {exc}",
            })
    summary = {
        "ok": all(bool(d.get("ok")) for d in deliveries),
        "enabled": True,
        "channels": [d["channel"] for d in deliveries],
        "deliveries": deliveries,
    }
    try:
        jsonl.append(config.paths.journal("trading"), {
            "kind": "trade.notification",
            "ts": now_iso(),
            "strategy_id": event.get("strategy_id"),
            "session_id": event.get("session_id"),
            "intent_id": event.get("intent_id"),
            "order_id": event.get("order_id"),
            "status": event.get("status"),
            "summary": summary,
        })
    except Exception:
        pass
    return summary


def format_trade_message(event: dict[str, Any]) -> str:
    status = _clean_str(event.get("status")) or "unknown"
    lines = [f"Nerya trade {status}"]
    _append(lines, "Strategy", event.get("strategy_id"))
    _append(lines, "Account", event.get("account_id"))
    _append(lines, "Market", event.get("market"))
    _append(lines, "Side", _clean_str(event.get("side")).upper())
    order_ids = event.get("order_ids")
    order_id = event.get("order_id")
    if isinstance(order_ids, list) and len(order_ids) > 1:
        _append(lines, "Orders", ", ".join(str(x) for x in order_ids if x))
    else:
        _append(lines, "Order", order_id)
    _append(lines, "Intent", event.get("intent_id"))
    _append(lines, "Plan", event.get("plan_id"))
    _append(lines, "Executor", event.get("executor_id"))
    _append(lines, "Session", event.get("session_id"))
    fill = _format_fill_line(event)
    if fill:
        lines.append(fill)
    notional = _float_or_none(event.get("notional_usd"))
    if notional is not None:
        lines.append(f"Notional: ${notional:,.2f}")
    fee = _float_or_none(event.get("fee_usd"))
    if fee is not None and fee:
        lines.append(f"Fee: ${fee:,.4f}")
    risk = event.get("risk_decision") if isinstance(event.get("risk_decision"), dict) else {}
    decision = _clean_str(risk.get("decision"))
    if decision:
        reasons = risk.get("reasons")
        suffix = ""
        if isinstance(reasons, list) and reasons:
            suffix = f" ({', '.join(str(r) for r in reasons[:3])})"
        lines.append(f"Risk: {decision}{suffix}")
    _append(lines, "Source", event.get("source"))
    return "\n".join(lines)


def _trade_notification_channels(config: Config) -> list[tuple[str, dict[str, Any]]]:
    doc = yaml_io.load(config.paths.messages_channels, default={}) or {}
    channels = doc.get("channels") if isinstance(doc, dict) else {}
    if not isinstance(channels, dict):
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for name, raw in channels.items():
        cfg = dict(raw or {})
        channel = str(name)
        if not _wants_trade_notifications(channel, cfg):
            continue
        if not _channel_is_configured_for_outbound(channel, cfg):
            continue
        out.append((channel, cfg))
    return out


def _wants_trade_notifications(channel: str, cfg: dict[str, Any]) -> bool:
    if cfg.get("disabled") is True or cfg.get("enabled") is False:
        return False
    if cfg.get("trade_notifications") is False or cfg.get("trades") is False:
        return False
    topics = cfg.get("topics")
    if isinstance(topics, list) and topics:
        normalized = {str(t).strip().lower() for t in topics}
        return bool(normalized & _TRADE_TOPICS)
    if cfg.get("trade_notifications") is True or cfg.get("trades") is True:
        return True
    # Operator expectation: once a real gateway channel is configured,
    # trade execution messages should flow unless the channel opts out.
    return _channel_kind(channel, cfg) not in _LOCAL_KINDS


def _channel_is_configured_for_outbound(channel: str, cfg: dict[str, Any]) -> bool:
    kind = _channel_kind(channel, cfg)
    if kind in _LOCAL_KINDS:
        return True
    if kind == "telegram":
        return bool((cfg.get("bot_token_ref") or cfg.get("token_ref")) and cfg.get("chat_id"))
    if kind == "discord":
        return bool(cfg.get("webhook_url_ref") or cfg.get("url_ref") or cfg.get("webhook_url"))
    if kind == "webhook":
        return bool(cfg.get("url") or cfg.get("url_ref") or cfg.get("webhook_url") or cfg.get("webhook_url_ref"))
    # Spec-aware fallback: a Feishu/WeCom/Slack channel that satisfies its
    # `secret_fields` (e.g. Feishu app_id+app_secret, WeCom corp_id+agent_id+secret,
    # Slack incoming_webhook_url) is configured for outbound even if it didn't
    # set a generic `webhook_url`. This keeps the trade-notification fan-out in
    # lockstep with `_gateway_channel_configured` on the API side.
    try:
        from .platforms import get_platform
        spec = get_platform(kind)
    except Exception:
        spec = None
    if spec is not None and spec.secret_fields:
        required = [f for f in spec.secret_fields if f.required]
        if required and all(cfg.get(f.key) or cfg.get(f.ref_key) for f in required):
            return True
    return bool(
        cfg.get("webhook_url")
        or cfg.get("webhook_url_ref")
        or cfg.get("incoming_webhook_url")
        or cfg.get("incoming_webhook_url_ref")
        or cfg.get("url")
        or cfg.get("url_ref")
        or cfg.get("bot_token_ref")
        or cfg.get("token_ref")
    )


def _channel_kind(channel: str, cfg: dict[str, Any]) -> str:
    return str(cfg.get("kind") or ("telegram" if channel == "telegram" else channel)).strip().lower()


def _asdict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "asdict"):
        try:
            data = value.asdict()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    try:
        data = asdict(value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _append(lines: list[str], label: str, value: Any) -> None:
    text = _clean_str(value)
    if text:
        lines.append(f"{label}: {text}")


def _format_fill_line(event: dict[str, Any]) -> str:
    size = _float_or_none(event.get("filled_size"))
    price = _float_or_none(event.get("avg_price"))
    if size is None and price is None:
        return ""
    if size is not None and price is not None:
        return f"Fill: {size:g} @ {price:g}"
    if size is not None:
        return f"Filled size: {size:g}"
    return f"Avg price: {price:g}"


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


__all__ = [
    "broadcast_trade_event",
    "event_from_executor_run",
    "event_from_order_result",
    "format_trade_message",
]
