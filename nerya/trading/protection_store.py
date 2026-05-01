"""Protection rule persistence + soft runtime evaluation.

every open position should have an auditable
:class:`ProtectionRule`. The store here owns the lifecycle of those
rules in SQLite. The :func:`evaluate` helper is the soft-runtime
trigger evaluator used by :class:`PositionProtectionExecutor`; it is
intentionally pure so the same logic runs in backtest / paper / live.

Hard-exchange and hybrid modes are supported via
:meth:`ProtectionStore.attach_exchange_orders` — the executor stores
each leg's exchange order id so we can cancel / replace them as the
position changes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from ..core.errors import IntentValidationError
from ..core.paths import WorkspacePaths
from ..db.sqlite import connect
from .order_intents import (
    PartialExitSpec,
    ProtectionRule,
    ProtectionStatus,
    StopLossSpec,
    TakeProfitSpec,
    TrailingStopSpec,
)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class ProtectionStore:
    def __init__(self, paths: WorkspacePaths):
        self.paths = paths
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.paths.db)
        return self._con

    def upsert(self, rule: ProtectionRule) -> ProtectionRule:
        if not rule.position_id:
            raise IntentValidationError("ProtectionRule requires position_id before persistence")
        con = self._con_lazy()
        rule.updated_at = _now_iso()
        con.execute(
            """
            INSERT INTO protection_rules (
                protection_id, position_id, executor_id, strategy_id, account_id,
                market, side, mode, status, trigger_source, time_limit_sec,
                rule_json, exchange_order_ids_json, created_at, updated_at,
                triggered_at, triggered_kind, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(protection_id) DO UPDATE SET
                executor_id    = excluded.executor_id,
                status         = excluded.status,
                rule_json      = excluded.rule_json,
                exchange_order_ids_json = excluded.exchange_order_ids_json,
                updated_at     = excluded.updated_at,
                triggered_at   = excluded.triggered_at,
                triggered_kind = excluded.triggered_kind,
                notes          = excluded.notes
            """,
            (
                rule.protection_id, rule.position_id, rule.executor_id,
                rule.strategy_id, rule.account_id, rule.market, rule.side,
                rule.mode, rule.status, rule.trigger_source, rule.time_limit_sec,
                json.dumps(rule.asdict()),
                json.dumps(rule.exchange_order_ids),
                _to_epoch(rule.created_at), _to_epoch(rule.updated_at),
                _to_epoch(rule.triggered_at) if rule.triggered_at else None,
                rule.triggered_kind, rule.notes,
            ),
        )
        return rule

    def get(self, protection_id: str) -> ProtectionRule | None:
        row = self._con_lazy().execute(
            "SELECT rule_json FROM protection_rules WHERE protection_id = ?",
            (protection_id,),
        ).fetchone()
        if not row:
            return None
        return _rule_from_json(json.loads(str(row["rule_json"])))

    def get_for_position(self, position_id: str) -> ProtectionRule | None:
        row = self._con_lazy().execute(
            """
            SELECT rule_json FROM protection_rules
             WHERE position_id = ?
               AND status NOT IN ('triggered', 'released', 'failed')
             ORDER BY created_at DESC LIMIT 1
            """,
            (position_id,),
        ).fetchone()
        if not row:
            return None
        return _rule_from_json(json.loads(str(row["rule_json"])))

    def list_active(self, *, account_id: str | None = None) -> list[ProtectionRule]:
        sql = (
            "SELECT rule_json FROM protection_rules "
            "WHERE status IN ('pending','armed','exchange_armed')"
        )
        params: tuple[Any, ...] = ()
        if account_id:
            sql += " AND account_id = ?"
            params = (account_id,)
        rows = self._con_lazy().execute(sql, params).fetchall()
        return [_rule_from_json(json.loads(str(r["rule_json"]))) for r in rows]

    def set_status(
        self,
        protection_id: str,
        status: ProtectionStatus,
        *,
        triggered_kind: str | None = None,
    ) -> None:
        rule = self.get(protection_id)
        if rule is None:
            return
        rule.status = status
        rule.updated_at = _now_iso()
        if status == "triggered":
            rule.triggered_at = _now_iso()
            if triggered_kind:
                rule.triggered_kind = triggered_kind
        self.upsert(rule)

    def attach_exchange_orders(
        self, protection_id: str, exchange_order_ids: dict[str, str]
    ) -> None:
        rule = self.get(protection_id)
        if rule is None:
            return
        merged = dict(rule.exchange_order_ids)
        merged.update(exchange_order_ids)
        rule.exchange_order_ids = merged
        if rule.status == "armed":
            rule.status = "exchange_armed"
        self.upsert(rule)


# ---------------------------------------------------------------------------
# Evaluation (pure)
# ---------------------------------------------------------------------------


@dataclass
class ProtectionTrigger:
    """Result of a soft-runtime evaluation tick."""

    fired: bool
    kind: Literal["take_profit", "stop_loss", "trailing_stop", "time_limit", "partial_exit", ""]
    close_pct: float = 1.0
    reason: str = ""

    @classmethod
    def none(cls) -> "ProtectionTrigger":
        return cls(fired=False, kind="", close_pct=0.0, reason="")


def evaluate(
    rule: ProtectionRule,
    *,
    entry_price: float,
    current_price: float,
    side: Literal["long", "short"],
    opened_at: float,
    now: float | None = None,
    high_water_mark: float | None = None,
) -> ProtectionTrigger:
    """Decide whether ``current_price`` triggers the rule.

    Returns ``ProtectionTrigger(fired=False)`` when nothing fires —
    callers should then update ``high_water_mark`` (for trailing
    stops) and tick again next round.

    The order of evaluation matches the intuitive runtime order:

    1. Time limit (cheapest, no price needed).
    2. Stop loss.
    3. Take profit.
    4. Trailing stop (only when ``high_water_mark`` has been updated).
    5. Partial exits (sorted by trigger ascending).

    Hard-exchange rules also call this helper as a *backup* trigger
    so a missing exchange ack doesn't leave us unprotected.
    """
    now = now if now is not None else time.time()
    sign = 1.0 if side == "long" else -1.0

    if rule.time_limit_sec and now - opened_at >= rule.time_limit_sec:
        return ProtectionTrigger(
            fired=True,
            kind="time_limit",
            close_pct=1.0,
            reason=f"time_limit_reached:{int(now - opened_at)}s",
        )

    pnl_pct = ((current_price - entry_price) / entry_price) * sign

    if rule.stop_loss is not None:
        if _stop_loss_hit(rule.stop_loss, entry_price=entry_price,
                          current_price=current_price, side=side, pnl_pct=pnl_pct):
            return ProtectionTrigger(
                fired=True, kind="stop_loss", close_pct=1.0,
                reason=f"stop_loss:{rule.stop_loss.type}:{rule.stop_loss.value}",
            )

    if rule.take_profit is not None:
        if _take_profit_hit(rule.take_profit, entry_price=entry_price,
                            current_price=current_price, side=side, pnl_pct=pnl_pct):
            return ProtectionTrigger(
                fired=True, kind="take_profit", close_pct=1.0,
                reason=f"take_profit:{rule.take_profit.type}:{rule.take_profit.value}",
            )

    if rule.trailing_stop is not None and high_water_mark is not None:
        if _trailing_hit(rule.trailing_stop, entry_price=entry_price,
                         current_price=current_price, side=side,
                         high_water_mark=high_water_mark):
            return ProtectionTrigger(
                fired=True, kind="trailing_stop", close_pct=1.0,
                reason=(
                    f"trailing_stop:trail_pct={rule.trailing_stop.trail_pct}"
                ),
            )

    for partial in sorted(rule.partial_exits or [], key=lambda p: p.trigger_pct):
        if pnl_pct >= partial.trigger_pct:
            return ProtectionTrigger(
                fired=True, kind="partial_exit", close_pct=partial.close_pct,
                reason=f"partial_exit:trigger={partial.trigger_pct}:close={partial.close_pct}",
            )

    return ProtectionTrigger.none()


def _stop_loss_hit(
    spec: StopLossSpec,
    *,
    entry_price: float,
    current_price: float,
    side: Literal["long", "short"],
    pnl_pct: float,
) -> bool:
    if spec.type == "pct":
        return pnl_pct <= -float(spec.value)
    if spec.type == "price":
        if side == "long":
            return current_price <= float(spec.value)
        return current_price >= float(spec.value)
    if spec.type == "atr":
        # Simplified: treat ATR as a price distance.
        if side == "long":
            return current_price <= entry_price - float(spec.value)
        return current_price >= entry_price + float(spec.value)
    if spec.type == "pnl_usd":
        # Without size context the soft check delegates to caller —
        # ``False`` means "let the executor compute USD PnL itself".
        return False
    return False


def _take_profit_hit(
    spec: TakeProfitSpec,
    *,
    entry_price: float,
    current_price: float,
    side: Literal["long", "short"],
    pnl_pct: float,
) -> bool:
    if spec.type == "pct":
        return pnl_pct >= float(spec.value)
    if spec.type == "price":
        if side == "long":
            return current_price >= float(spec.value)
        return current_price <= float(spec.value)
    if spec.type == "r_multiple":
        # Without an R distance the soft check delegates upstream.
        return False
    if spec.type == "pnl_usd":
        return False
    return False


def _trailing_hit(
    spec: TrailingStopSpec,
    *,
    entry_price: float,
    current_price: float,
    side: Literal["long", "short"],
    high_water_mark: float,
) -> bool:
    activation = float(spec.activation_pct or 0.0)
    trail = float(spec.trail_pct or 0.0)
    if trail <= 0:
        return False
    if side == "long":
        peak_pnl = (high_water_mark - entry_price) / entry_price
        if peak_pnl < activation:
            return False
        return current_price <= high_water_mark * (1.0 - trail)
    # short
    trough_pnl = (entry_price - high_water_mark) / entry_price
    if trough_pnl < activation:
        return False
    return current_price >= high_water_mark * (1.0 + trail)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule_from_json(payload: dict[str, Any]) -> ProtectionRule:
    sl = payload.get("stop_loss")
    tp = payload.get("take_profit")
    trail = payload.get("trailing_stop")
    partials = payload.get("partial_exits") or []
    return ProtectionRule(
        protection_id=str(payload.get("protection_id")),
        position_id=str(payload.get("position_id") or ""),
        executor_id=str(payload.get("executor_id") or ""),
        strategy_id=str(payload.get("strategy_id") or ""),
        account_id=str(payload.get("account_id") or ""),
        market=str(payload.get("market") or ""),
        side=str(payload.get("side") or "long"),  # type: ignore[arg-type]
        mode=str(payload.get("mode") or "soft_runtime"),  # type: ignore[arg-type]
        stop_loss=StopLossSpec(**sl) if isinstance(sl, dict) else None,
        take_profit=TakeProfitSpec(**tp) if isinstance(tp, dict) else None,
        time_limit_sec=payload.get("time_limit_sec"),
        trailing_stop=TrailingStopSpec(**trail) if isinstance(trail, dict) else None,
        partial_exits=[PartialExitSpec(**p) for p in partials if isinstance(p, dict)],
        status=str(payload.get("status") or "pending"),  # type: ignore[arg-type]
        trigger_source=str(payload.get("trigger_source") or "mark"),  # type: ignore[arg-type]
        exchange_order_ids=dict(payload.get("exchange_order_ids") or {}),
        created_at=str(payload.get("created_at") or _now_iso()),
        updated_at=str(payload.get("updated_at") or _now_iso()),
        triggered_at=payload.get("triggered_at"),
        triggered_kind=payload.get("triggered_kind"),
        notes=str(payload.get("notes") or ""),
    )


def _now_iso() -> str:
    from ..core.time import now_iso
    return now_iso()


def _to_epoch(iso_or_epoch: str | float | None) -> float | None:
    if iso_or_epoch is None:
        return None
    if isinstance(iso_or_epoch, (int, float)):
        return float(iso_or_epoch)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(iso_or_epoch).replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


__all__ = [
    "ProtectionStore",
    "ProtectionTrigger",
    "evaluate",
]
