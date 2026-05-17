"""Trading native tools — promote core trading actions out of the legacy bridge.

The trading skill is the safety-critical one: every order, risk check,
and kill-switch toggle must go through the runtime journals + risk
engine + approval gate. We expose them as native tools here so the
agent can call them directly (without ``runtime.call``) and so the
permission engine sees them with their real risk class
(``WRITE`` / ``EXEC`` / ``DANGEROUS`` for kill-switch + intent submit).

All handlers use existing domain modules — :class:`RiskGate`,
:class:`ApprovalGate`, :class:`ExecutionEngine`, ``StateStore`` — so
the live-trading invariants stay where they were proven, not in this
adapter layer.

Hand-picked surface (mirrors the legacy ``trading_skill`` /
``portfolio_skill`` / ``risk_skill`` actions Nerya ships today):

* :func:`portfolio_summary_handler` — accounts + virtual ledger snapshot.
* :func:`portfolio_positions_handler` — open positions only.
* :func:`portfolio_pnl_handler` — realised + unrealised PnL.
* :func:`risk_check_handler` — read-only ``RiskGate.evaluate``.
* :func:`kill_switch_set_handler` — toggle the runtime kill-switch.
* :func:`trade_intent_submit_handler` — full intent → risk →
  execute pipeline (with snapshot resolution + journal writes).
* :func:`strategy_list_handler` / :func:`strategy_view_handler` —
  read strategy specs.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path as _Path
from typing import Any

from ...core import jsonl
from ...core.config import Config
from ...core.redaction import redact_dict
from ...core.time import now_iso
from ...core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
    resolve_allow_mock,
)
from ...strategy_history import open_session, store as history_store, track_outcome
from ...trading import portfolio as portfolio_mod
from ...trading.accounts import load_accounts
from ...core.errors import ApprovalPending
from ...trading.approval import ApprovalGate
from ...trading.execution import ExecutionEngine
from ...trading.intents import TradeIntent
from ...trading.risk import RiskGate
from ...trading.strategies import Strategy, list_strategies, load_strategy
from ...trading.virtual_ledger import open_ledger
from ...workspace.state_store import StateStore
from ..types import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


def _resolve_market_snapshot(
    config: Config,
    intent: TradeIntent,
    *,
    supplied: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reduced version of the legacy ``_resolve_live_market_snapshot``.

    The native tool path doesn't have a ``ctx`` to cache connectors on,
    so we keep the resolution simple:

    * caller-supplied snapshot wins (tagged ``live`` if untagged),
    * else degraded envelope referencing ``intent.limit_price`` so the
      execution engine rejects rather than fabricating a fill.

    The ``markets`` skill (``get_quote.py`` / ``get_book.py`` scripts)
    is the agent's path to a fresher snapshot — they can be invoked via
    ``run_shell`` before submitting an intent if the model wants to
    upgrade the snapshot.
    """

    venue_hint = intent.market.split(":", 1)[0].lower() if ":" in intent.market else ""
    if isinstance(supplied, dict) and supplied:
        snap = dict(supplied)
        if "_envelope" not in snap:
            snap["_envelope"] = live_envelope(
                source=str(snap.get("source", venue_hint or "caller")),
                venue=venue_hint,
            ).as_dict()
        return snap
    if resolve_allow_mock(None, config):
        try:
            from ...connectors.mock_exchange import MockExchange

            tk = MockExchange().get_ticker(intent.market)
            return {
                "price": float(tk.mid),
                "age_s": 0,
                "_envelope": mock_envelope(source="mock", venue=venue_hint).as_dict(),
            }
        except Exception:
            pass
    return {
        "price": intent.limit_price or 0.0,
        "age_s": 0,
        "_envelope": degraded_envelope(
            "market_snapshot",
            error="no_live_snapshot_supplied",
            venue=venue_hint,
        ).as_dict(),
    }


def _strategy_to_dict(s: Strategy) -> dict[str, Any]:
    """Serialize :class:`Strategy` (which has no ``asdict()``) for JSON.

    ``dataclasses.asdict`` recurses into ``StrategyLimits`` automatically;
    we only need to coerce the ``Path`` and the read-only
    ``is_tradable`` property.
    """

    raw = dataclasses.asdict(s)
    if isinstance(raw.get("path"), _Path):
        raw["path"] = str(raw["path"])
    raw["is_tradable"] = bool(s.is_tradable)
    return raw


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


PORTFOLIO_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}

PORTFOLIO_POSITIONS_SCHEMA: dict[str, Any] = PORTFOLIO_SUMMARY_SCHEMA
PORTFOLIO_PNL_SCHEMA: dict[str, Any] = PORTFOLIO_SUMMARY_SCHEMA

VIRTUAL_LEDGER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "account_id": {"type": "string"},
    },
    "required": ["account_id"],
}

RISK_CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "object",
            "description": (
                "Trade intent payload (see TradeIntent). intent_id is "
                "auto-generated when omitted."
            ),
        },
        "market_snapshot": {
            "type": "object",
            "description": "Optional snapshot {price, age_s, _envelope}.",
        },
    },
    "required": ["intent"],
}

KILL_SWITCH_SET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "enabled": {
            "type": "boolean",
            "description": "True to engage the kill switch (block live orders).",
        },
        "reason": {
            "type": "string",
            "description": "Operator-readable reason; logged with the toggle.",
        },
    },
    "required": ["enabled"],
}

TRADE_INTENT_SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {"type": "string"},
        "account_id": {"type": "string"},
        "market": {
            "type": "string",
            "description": "venue:symbol (e.g. binance:BTC/USDT).",
        },
        "side": {"type": "string", "enum": ["buy", "sell"]},
        "size": {"type": "number"},
        "size_unit": {"type": "string", "enum": ["base", "quote", "usd"]},
        "order_type": {
            "type": "string",
            "enum": ["market", "limit", "stop", "stop_limit"],
        },
        "limit_price": {"type": "number"},
        "stop_price": {"type": "number"},
        "time_in_force": {
            "type": "string",
            "enum": ["gtc", "ioc", "fok", "post_only"],
            "default": "gtc",
        },
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "source": {"type": "string"},
        "trigger_event_id": {"type": "string"},
        "market_snapshot": {"type": "object"},
        # Optional bracket TP/SL. When present the order routes through
        # the TradePlan pipeline (``TradingAPI.open_position``) and
        # arms the protection at the exchange (live) or in-process
        # protection executor (paper/shadow) in one atomic step.
        # Agents SHOULD include this for every fresh entry on a CEX —
        # bare market orders without a stop are a known way to leak
        # capital during overnight gaps.
        "protection": {
            "type": "object",
            "description": (
                "Optional bracket TP/SL. Triggers the TradePlan pipeline "
                "(open_position) when ``side='buy'`` — for SHORT entries "
                "use ``plan_action='open_short'`` alongside. Each child "
                "spec accepts ``type`` (pct|price|atr|pnl_usd|r_multiple) "
                "and ``value``."
            ),
            "properties": {
                "stop_loss": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["pct", "price", "atr", "pnl_usd"],
                        },
                        "value": {"type": "number"},
                    },
                    "required": ["type", "value"],
                },
                "take_profit": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["pct", "price", "r_multiple", "pnl_usd"],
                        },
                        "value": {"type": "number"},
                    },
                    "required": ["type", "value"],
                },
                "trailing_stop": {
                    "type": "object",
                    "properties": {
                        "activation_pct": {"type": "number"},
                        "trail_pct": {"type": "number"},
                    },
                },
                "mode": {
                    "type": "string",
                    "enum": ["hard_exchange", "soft_runtime", "hybrid"],
                    "default": "hybrid",
                },
            },
        },
        # When provided alongside ``protection``, controls which
        # TradePlan action is dispatched. Defaults to ``open_long`` for
        # ``side='buy'`` and ``open_short`` for ``side='sell'``.
        "plan_action": {
            "type": "string",
            "enum": [
                "open_long",
                "open_short",
                "close_position",
                "reduce_position",
                "scale_in",
            ],
        },
    },
    "required": ["account_id", "market", "side", "size", "size_unit", "order_type"],
}

STRATEGY_LIST_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
STRATEGY_VIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"strategy_id": {"type": "string"}},
    "required": ["strategy_id"],
}
STRATEGY_HISTORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy_id": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "default": 20},
    },
    "required": ["strategy_id"],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _usage_error(call: ToolCall, message: str) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=ToolErrorKind.SCHEMA_VALIDATION, message=message,
        ),
    )


def _build_intent(args: dict[str, Any], *, default_strategy: str = "manual_agent") -> TradeIntent:
    spec = dict(args)
    if "intent_id" in spec:
        return TradeIntent(**spec)
    spec.setdefault("strategy_id", default_strategy)
    spec.setdefault("source", "agent")
    return TradeIntent.new(**spec)


# ---------------------------------------------------------------------------
# Handlers — read-only
# ---------------------------------------------------------------------------


def portfolio_summary_handler(call: ToolCall, *, config: Config) -> ToolResult:
    summary = portfolio_mod.get_portfolio_summary(config.paths)
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=summary)


def portfolio_positions_handler(call: ToolCall, *, config: Config) -> ToolResult:
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"positions": portfolio_mod.get_positions(config.paths)},
    )


def portfolio_pnl_handler(call: ToolCall, *, config: Config) -> ToolResult:
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data=portfolio_mod.get_pnl(config.paths),
    )


def virtual_ledger_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    aid = (args.get("account_id") or "").strip()
    if not aid:
        return _usage_error(call, "account_id is required")
    accts = load_accounts(config.paths)
    if aid not in accts:
        return ToolResult.from_json(
            tool_use_id=call.id, name=call.name,
            data={"account_id": aid, "found": False},
        )
    a = accts[aid]
    led = open_ledger(config.paths, a.id, a.initial_balance_usd)
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name,
        data={"account_id": a.id, "found": True, **led.snapshot()},
    )


def risk_check_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    raw = args.get("intent")
    if not isinstance(raw, dict) or not raw:
        return _usage_error(call, "intent (dict) is required")
    try:
        intent = _build_intent(raw)
    except Exception as exc:
        return _usage_error(call, f"invalid intent: {type(exc).__name__}: {exc}")
    snapshot = args.get("market_snapshot") if isinstance(args.get("market_snapshot"), dict) else None
    decision = RiskGate(config).evaluate(intent, market_snapshot=snapshot)
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name,
        data={"intent": intent.asdict(), "risk_decision": decision.asdict()},
    )


def strategy_list_handler(call: ToolCall, *, config: Config) -> ToolResult:
    rows = list_strategies(config.paths)
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name,
        data={
            "count": len(rows),
            "strategies": [_strategy_to_dict(s) for s in rows],
        },
    )


def strategy_view_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    sid = (args.get("strategy_id") or "").strip()
    if not sid:
        return _usage_error(call, "strategy_id is required")
    try:
        spec = _strategy_to_dict(load_strategy(config.paths, sid))
    except Exception as exc:
        return _usage_error(call, f"strategy_unknown: {type(exc).__name__}: {exc}")
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=spec)


def strategy_history_handler(call: ToolCall, *, config: Config) -> ToolResult:
    args = call.arguments or {}
    sid = (args.get("strategy_id") or "").strip()
    if not sid:
        return _usage_error(call, "strategy_id is required")
    limit = max(1, int(args.get("limit") or 20))
    out: dict[str, Any] = {"strategy_id": sid, "ledgers": {}}
    for name in (
        "triggers", "intents", "risk", "orders", "fills",
        "messages", "reviews",
    ):
        try:
            rows = history_store.read_ledger(config.paths, sid, name)
        except Exception:
            rows = []
        out["ledgers"][name] = {"count": len(rows), "tail": rows[-limit:]}
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=out)


# ---------------------------------------------------------------------------
# Handlers — write / dangerous
# ---------------------------------------------------------------------------


def kill_switch_set_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """Engage / release the runtime kill switch.

    Mirrors :func:`risk_skill.enable_kill_switch` /
    :func:`risk_skill.disable_kill_switch`. Persists through
    :class:`StateStore` and updates the in-memory config so the
    next :class:`RiskGate` call sees it.
    """

    args = call.arguments or {}
    if "enabled" not in args:
        return _usage_error(call, "enabled (bool) is required")
    enabled = bool(args.get("enabled"))
    reason = str(args.get("reason") or "")
    store = StateStore(config.paths.runtime_state)
    store.set("kill_switch", enabled)
    if enabled:
        store.set("kill_switch_reason", reason)
    runtime = config.data.setdefault("runtime", {})
    runtime["kill_switch"] = enabled
    jsonl.append(config.paths.journal("trading"), {
        "kind": "kill_switch.set",
        "ts": now_iso(),
        "enabled": enabled,
        "reason": reason,
    })
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name,
        data={"kill_switch": enabled, "reason": reason if enabled else None},
    )


def trade_intent_submit_handler(
    call: ToolCall,
    *,
    config: Config,
    default_strategy: str = "manual_agent",
    default_source: str = "agent:native",
) -> ToolResult:
    """Submit a trade intent through risk → approval → execution.

    Thin adapter — the canonical pipeline lives in
    :func:`nerya.trading.submit.submit_trade_intent` for bare intents,
    and :class:`nerya.sdk.trading_api.TradingAPI` for plan-shaped
    intents that ship a bracket protection block.

    Routing
    -------
    * ``args["protection"]`` present →
      :meth:`TradingAPI.open_position`/``close_position``/``reduce_position``
      (depending on ``plan_action``). The TradePlan path arms the
      bracket at the exchange (live) or the in-process executor
      (paper/shadow) atomically with the entry order. This is the
      preferred Agent path for fresh entries — bare market orders
      without a stop are a known way to leak capital.
    * Otherwise → legacy ``submit_trade_intent`` (bare order).

    Approval-pending and risk-rejected outcomes return successfully
    (the verdict is inside ``risk_decision``); only true execution /
    validation errors come back as a ``ToolError``.
    """

    from ...trading.submit import submit_trade_intent as _submit

    args = call.arguments or {}
    if not args:
        return _usage_error(call, "intent fields required (account_id, market, side, ...)")
    spec = dict(args)
    snapshot_in = spec.pop("market_snapshot", None)
    protection = spec.pop("protection", None)
    plan_action = spec.pop("plan_action", None)

    # --- Bracket-aware path: route through TradingAPI / TradePlan ---
    if isinstance(protection, dict) and protection:
        return _submit_with_protection(
            call,
            config=config,
            spec=spec,
            protection=protection,
            plan_action=plan_action,
            market_snapshot=snapshot_in if isinstance(snapshot_in, dict) else None,
            default_strategy=default_strategy,
            default_source=default_source,
        )

    # --- Legacy bare-intent path ---
    try:
        envelope = _submit(
            config,
            spec=spec,
            market_snapshot=snapshot_in if isinstance(snapshot_in, dict) else None,
            default_strategy=spec.get("strategy_id") or default_strategy,
            default_source=spec.get("source") or default_source,
        )
    except (TypeError, ValueError) as exc:
        return _usage_error(call, f"invalid intent: {type(exc).__name__}: {exc}")
    except Exception as exc:
        return ToolResult.from_error(
            tool_use_id=call.id, name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            ),
        )
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name, data=envelope,
    )


def _submit_with_protection(
    call: ToolCall,
    *,
    config: Config,
    spec: dict[str, Any],
    protection: dict[str, Any],
    plan_action: str | None,
    market_snapshot: dict[str, Any] | None,
    default_strategy: str,
    default_source: str,
) -> ToolResult:
    """Dispatch the protected intent through ``TradingAPI``.

    Maps the Agent-facing flat ``(side, size, size_unit)`` shape onto
    the structured ``(side: long|short, sizing: SizingPolicy)`` shape
    the TradePlan pipeline expects, then delegates to the matching
    ``TradingAPI`` method:

    * ``plan_action="open_long"`` (default for ``side="buy"``) /
      ``plan_action="open_short"`` (default for ``side="sell"``) →
      :meth:`TradingAPI.open_position` — places the entry + arms the
      bracket atomically.
    * ``plan_action="close_position"`` →
      :meth:`TradingAPI.close_position` — releases the bracket and
      forces ``SizingPolicy(close_all)``. Sizing supplied by the
      caller is ignored (close_all already determines size).
    * ``plan_action="reduce_position"`` →
      :meth:`TradingAPI.reduce_position` — trims by either
      ``size`` (treated as ``fixed_base``) or by a fraction the
      caller passes inside ``protection.reduce_pct``.

    All other ``plan_action`` values fall back to ``open_position``
    so an Agent that supplies a protection block always gets a
    bracketed entry.
    """

    from ...sdk.trading_api import TradingAPI
    from ...skills.kernel import SkillKernel

    side_order = str(spec.get("side") or "").strip().lower()
    if side_order not in ("buy", "sell"):
        return _usage_error(
            call,
            f"side must be 'buy' or 'sell' when protection is set; got {side_order!r}",
        )
    # Derive the *position* side from the order side. ``plan_action``
    # may override this when the Agent explicitly knows it's closing
    # an existing position.
    position_side = "long" if side_order == "buy" else "short"

    size_raw = spec.get("size")
    size_unit = str(spec.get("size_unit") or "usd").strip().lower()
    try:
        size_val = float(size_raw)
    except (TypeError, ValueError):
        return _usage_error(call, f"size must be numeric; got {size_raw!r}")
    if size_val <= 0 and plan_action not in ("close_position",):
        return _usage_error(call, "size must be positive for an open/reduce intent")

    sizing: dict[str, Any]
    if size_unit == "usd":
        sizing = {"method": "fixed_usd", "fixed_usd": size_val}
    elif size_unit in ("base", "quote"):
        # The TradePlan SizingPolicy doesn't have a separate quote
        # method — quote ≈ base for spot, BudgetChecker resolves
        # final notional from the snapshot. Treat both as fixed_base
        # at the policy layer.
        sizing = {"method": "fixed_base", "fixed_base": size_val}
    else:
        return _usage_error(
            call, f"size_unit must be one of base/quote/usd; got {size_unit!r}"
        )

    account_id = str(spec.get("account_id") or "").strip()
    if not account_id:
        return _usage_error(call, "account_id is required")
    market = str(spec.get("market") or "").strip()
    if not market:
        return _usage_error(call, "market is required")

    strategy_id = str(spec.get("strategy_id") or default_strategy)
    confidence = float(spec.get("confidence") or 0.0)
    reasoning_ref = str(spec.get("reasoning") or "")
    trigger_event_id = spec.get("trigger_event_id")
    source = str(spec.get("source") or default_source)

    api = TradingAPI(config=config, skills=SkillKernel.boot(config))

    try:
        action = (plan_action or "").strip() or (
            "open_long" if position_side == "long" else "open_short"
        )
        if action == "close_position":
            envelope = api.close_position(
                strategy_id=strategy_id,
                account_id=account_id,
                market=market,
                side=position_side,  # type: ignore[arg-type]
                confidence=confidence,
                reasoning_ref=reasoning_ref,
                source=source,  # type: ignore[arg-type]
                market_snapshot=market_snapshot,
            )
        elif action == "reduce_position":
            envelope = api.reduce_position(
                strategy_id=strategy_id,
                account_id=account_id,
                market=market,
                side=position_side,  # type: ignore[arg-type]
                fixed_base=size_val if size_unit in ("base", "quote") else None,
                reduce_pct=(
                    float(protection.get("reduce_pct"))
                    if protection.get("reduce_pct")
                    else None
                ),
                confidence=confidence,
                reasoning_ref=reasoning_ref,
                source=source,  # type: ignore[arg-type]
                market_snapshot=market_snapshot,
            )
        else:
            # open_long / open_short / scale_in / fallback → open_position
            # ``open_short`` flips position_side regardless of the
            # caller-side ``side``.
            if action == "open_short":
                position_side = "short"
            elif action == "open_long":
                position_side = "long"
            envelope = api.open_position(
                strategy_id=strategy_id,
                account_id=account_id,
                market=market,
                side=position_side,  # type: ignore[arg-type]
                sizing=sizing,
                protection=protection,
                confidence=confidence,
                reasoning_ref=reasoning_ref,
                trigger_event_id=trigger_event_id,
                source=source,  # type: ignore[arg-type]
                market_snapshot=market_snapshot,
            )
    except (TypeError, ValueError) as exc:
        return _usage_error(call, f"invalid plan: {type(exc).__name__}: {exc}")
    except Exception as exc:
        return ToolResult.from_error(
            tool_use_id=call.id, name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            ),
        )
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name, data=envelope,
    )


__all__ = [
    "KILL_SWITCH_SET_SCHEMA",
    "PORTFOLIO_PNL_SCHEMA",
    "PORTFOLIO_POSITIONS_SCHEMA",
    "PORTFOLIO_SUMMARY_SCHEMA",
    "RISK_CHECK_SCHEMA",
    "STRATEGY_HISTORY_SCHEMA",
    "STRATEGY_LIST_SCHEMA",
    "STRATEGY_VIEW_SCHEMA",
    "TRADE_INTENT_SUBMIT_SCHEMA",
    "VIRTUAL_LEDGER_SCHEMA",
    "kill_switch_set_handler",
    "portfolio_pnl_handler",
    "portfolio_positions_handler",
    "portfolio_summary_handler",
    "risk_check_handler",
    "strategy_history_handler",
    "strategy_list_handler",
    "strategy_view_handler",
    "trade_intent_submit_handler",
    "virtual_ledger_handler",
]
