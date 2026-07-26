"""Position protection executor.

Watches a position against its :class:`ProtectionRule`. Default mode
is *soft runtime* — the executor evaluates the rule against the
current mark price every tick and, when a trigger fires, spawns a
flatten ``MarketOrderExecutor`` to close the position.

The executor is small on purpose. Hard-exchange and hybrid modes
hook in here via :meth:`prepare`/:meth:`step` extensions later. For now we always run the soft path,
which is the only mode guaranteed to work on every CCXT venue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from ..order_intents import (
    OrderCandidate,
    ProtectionRule,
    PartialExitSpec,
    StopLossSpec,
    TakeProfitSpec,
    TrailingStopSpec,
)
from ..position_book import PositionBook
from ..protection_store import ProtectionStore, evaluate
from .base import Executor, ExecutorConfig

log = logging.getLogger(__name__)


@dataclass
class ProtectionExecutorConfig(ExecutorConfig):
    rule: dict[str, Any] = field(default_factory=dict)
    position_id: str = ""


class PositionProtectionExecutor(Executor):
    kind = "position_protection"

    def prepare(self) -> None:
        if not self.run.position_id:
            self.transition("rejected", close_type="failed")
            self.store_result({"reason": "no_position_id"})
            return
        # ``high_water_mark`` lives on the run for trailing stops so
        # state survives crash/restart.
        self.run.result_json.setdefault("high_water_mark", None)
        self.transition("ready")

    def step(self) -> bool:
        if self.run.state in ("rejected", "failed", "done", "canceled"):
            return True

        store = ProtectionStore(self.paths)
        rule = store.get_for_position(self.run.position_id or "") or _rule_from_config(
            self.run.config_json or {}
        )
        if rule is None:
            self.transition("done", close_type="filled")
            return True

        book = PositionBook(self.paths)
        position = book.get_by_id(self.run.position_id or "")
        if position is None or not position.is_open:
            store.set_status(rule.protection_id, "released")
            self.transition("done", close_type="filled")
            return True

        # Mark price source. Prefer a live connector mark; fall back to
        # the position's stored mark, then entry price. A stale mark is
        # dangerous for a soft stop-loss so we try the venue first.
        current_price = self._live_mark_price(rule) or position.mark_price or position.avg_entry_price
        if current_price <= 0:
            self.transition("working")
            return False

        prior_high = self.run.result_json.get("high_water_mark")
        new_high = _update_high_water(rule.side, current_price, prior_high)
        if new_high != prior_high:
            self.run.result_json["high_water_mark"] = new_high

        trigger = evaluate(
            rule,
            entry_price=position.avg_entry_price,
            current_price=current_price,
            side=rule.side,
            opened_at=position.opened_at,
            high_water_mark=new_high,
        )
        if not trigger.fired:
            self.transition("working")
            return False

        # Trigger fired — spin up a flatten market order. We don't
        # need the orchestrator here because paper mode is
        # synchronous; live mode flattens via a fresh executor row,
        # which the orchestrator picks up on the next tick.
        from .market_order import MarketOrderExecutor, MarketOrderConfig

        flatten_side = "sell" if rule.side == "long" else "buy"
        close_size = abs(position.size_base) * float(trigger.close_pct or 1.0)
        candidate = OrderCandidate(
            account_id=position.account_id,
            strategy_id=position.strategy_id,
            market=position.market,
            side=flatten_side,
            order_type="market",
            size_base=close_size,
            notional_usd=close_size * current_price,
            reduce_only=True,
        )
        flatten_cfg = MarketOrderConfig(
            kind="market_order",
            account_id=position.account_id,
            strategy_id=position.strategy_id,
            market=position.market,
            candidate=candidate.asdict(),
        )
        flattener = MarketOrderExecutor.new(
            account_id=position.account_id,
            strategy_id=position.strategy_id,
            market=position.market,
            config=flatten_cfg,
            paths=self.paths,
            position_id=position.position_id,
        )
        # Drive the flattener until terminal — paper mode is fully
        # synchronous; live mode would normally come back here on the
        # next tick because the flatten exec needs to poll itself.
        flattener.prepare()
        flattener.transition("ready")
        flattener.step()

        store.set_status(rule.protection_id, "triggered", triggered_kind=trigger.kind)
        self.store_result({
            "trigger_kind": trigger.kind,
            "trigger_reason": trigger.reason,
            "close_size_base": close_size,
            "flatten_executor_id": flattener.run.executor_id,
        })
        self.transition("done", close_type=_trigger_to_close_type(trigger.kind))
        return True

    def _live_mark_price(self, rule) -> float:
        """Best-effort live mark from the venue.

        For ``exchange_armed`` / ``hybrid`` rules the venue enforces the
        bracket natively, so the executor only needs the mark for
        monitoring/audit. For ``soft_runtime`` rules the mark drives the
        stop, so a live price is safety-critical. We swallow connector
        failures and let the caller fall back to the stored mark.
        """
        try:
            from ...connectors import ConnectorRegistry
            from ..accounts import get_account_profile
            profile = get_account_profile(self.paths, rule.account_id)
            registry = ConnectorRegistry(workspace=self.paths.root)
            legacy_account = profile.to_connector_account()
            conn = registry.get(profile.id, legacy_account.connector_cfg())
            return float(conn.get_mark_price(rule.market) or 0.0)
        except Exception:
            return 0.0

        prior_high = self.run.result_json.get("high_water_mark")
        new_high = _update_high_water(rule.side, current_price, prior_high)
        if new_high != prior_high:
            self.run.result_json["high_water_mark"] = new_high

        trigger = evaluate(
            rule,
            entry_price=position.avg_entry_price,
            current_price=current_price,
            side=rule.side,
            opened_at=position.opened_at,
            high_water_mark=new_high,
        )
        if not trigger.fired:
            self.transition("working")
            return False

        # Trigger fired — spin up a flatten market order. We don't
        # need the orchestrator here because paper mode is
        # synchronous; live mode flattens via a fresh executor row,
        # which the orchestrator picks up on the next tick.
        from .market_order import MarketOrderExecutor, MarketOrderConfig

        flatten_side = "sell" if rule.side == "long" else "buy"
        close_size = abs(position.size_base) * float(trigger.close_pct or 1.0)
        candidate = OrderCandidate(
            account_id=position.account_id,
            strategy_id=position.strategy_id,
            market=position.market,
            side=flatten_side,
            order_type="market",
            size_base=close_size,
            notional_usd=close_size * current_price,
            reduce_only=True,
        )
        flatten_cfg = MarketOrderConfig(
            kind="market_order",
            account_id=position.account_id,
            strategy_id=position.strategy_id,
            market=position.market,
            candidate=candidate.asdict(),
        )
        flattener = MarketOrderExecutor.new(
            account_id=position.account_id,
            strategy_id=position.strategy_id,
            market=position.market,
            config=flatten_cfg,
            paths=self.paths,
            position_id=position.position_id,
        )
        # Drive the flattener until terminal — paper mode is fully
        # synchronous; live mode would normally come back here on the
        # next tick because the flatten exec needs to poll itself.
        flattener.prepare()
        flattener.transition("ready")
        flattener.step()

        store.set_status(rule.protection_id, "triggered", triggered_kind=trigger.kind)
        self.store_result({
            "trigger_kind": trigger.kind,
            "trigger_reason": trigger.reason,
            "close_size_base": close_size,
            "flatten_executor_id": flattener.run.executor_id,
        })
        self.transition("done", close_type=_trigger_to_close_type(trigger.kind))
        return True

    def on_cancel(self) -> None:
        store = ProtectionStore(self.paths)
        rule_id = self.run.protection_id
        if rule_id:
            store.set_status(rule_id, "released")


def _update_high_water(
    side: Literal["long", "short"], price: float, prior: float | None
) -> float:
    if prior is None:
        return float(price)
    if side == "long":
        return max(float(prior), float(price))
    return min(float(prior), float(price))


def _rule_from_config(config_json: dict[str, Any]) -> ProtectionRule | None:
    raw = config_json.get("rule")
    if not isinstance(raw, dict):
        return None
    sl = raw.get("stop_loss")
    tp = raw.get("take_profit")
    trail = raw.get("trailing_stop")
    partials = raw.get("partial_exits") or []
    return ProtectionRule(
        protection_id=str(raw.get("protection_id")),
        position_id=str(raw.get("position_id") or ""),
        executor_id=str(raw.get("executor_id") or ""),
        strategy_id=str(raw.get("strategy_id") or ""),
        account_id=str(raw.get("account_id") or ""),
        market=str(raw.get("market") or ""),
        side=str(raw.get("side") or "long"),  # type: ignore[arg-type]
        mode=str(raw.get("mode") or "soft_runtime"),  # type: ignore[arg-type]
        stop_loss=StopLossSpec(**sl) if isinstance(sl, dict) else None,
        take_profit=TakeProfitSpec(**tp) if isinstance(tp, dict) else None,
        time_limit_sec=raw.get("time_limit_sec"),
        trailing_stop=TrailingStopSpec(**trail) if isinstance(trail, dict) else None,
        partial_exits=[PartialExitSpec(**p) for p in partials if isinstance(p, dict)],
        status=str(raw.get("status") or "armed"),  # type: ignore[arg-type]
    )


def _trigger_to_close_type(kind: str) -> str:
    if kind in ("stop_loss", "take_profit", "trailing_stop", "time_limit"):
        return kind
    return "filled"


__all__ = ["PositionProtectionExecutor", "ProtectionExecutorConfig"]
