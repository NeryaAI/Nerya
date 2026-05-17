"""Executor orchestrator.

Owns persistence and lifecycle of every :class:`Executor` instance.

Two top-level entry points:

* :meth:`ExecutorOrchestrator.create_market_order` — convenience for
  the new TradePlan path.
* :meth:`ExecutorOrchestrator.run_once` — one tick of every active
  executor; safe to call repeatedly. ``run_until_terminal`` is the
  test-friendly variant that drives a single executor until it hits a
  terminal state with a step cap.

State lives entirely in the ``executor_runs`` table introduced by
migration v3, so a runtime crash + restart can resume any executor by
reading the row back.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ...core.config import Config
from ...core.paths import WorkspacePaths
from ...db.sqlite import connect
from ..order_intents import OrderCandidate, ProtectionRule
from .base import (
    Executor,
    ExecutorConfig,
    ExecutorKind,
    ExecutorRun,
    ExecutorState,
    TERMINAL_EXECUTOR_STATES,
)

log = logging.getLogger(__name__)


# Map kind -> Executor subclass. Populated lazily to avoid a circular
# import at module load time.
def _registry() -> dict[ExecutorKind, type[Executor]]:
    from .market_order import MarketOrderExecutor
    from .position_protection import PositionProtectionExecutor

    return {
        "market_order": MarketOrderExecutor,
        "position_protection": PositionProtectionExecutor,
    }


class ExecutorOrchestrator:
    def __init__(self, config: Config, *, max_steps_per_tick: int = 64):
        self.config = config
        self.paths: WorkspacePaths = config.paths
        self.max_steps_per_tick = int(max_steps_per_tick)
        self._con = None

    # -- persistence ------------------------------------------------------------
    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.paths.db)
        return self._con

    def _persist(self, run: ExecutorRun) -> None:
        con = self._con_lazy()
        con.execute(
            """
            INSERT INTO executor_runs (
                executor_id, kind, account_id, strategy_id, market, state,
                close_type, retries, last_heartbeat, plan_json, config_json,
                result_json, order_ids_json, reservation_ids_json,
                position_id, protection_id, intent_id, plan_id,
                created_at, updated_at, terminal_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(executor_id) DO UPDATE SET
                state          = excluded.state,
                close_type     = excluded.close_type,
                retries        = excluded.retries,
                last_heartbeat = excluded.last_heartbeat,
                plan_json      = excluded.plan_json,
                config_json    = excluded.config_json,
                result_json    = excluded.result_json,
                order_ids_json = excluded.order_ids_json,
                reservation_ids_json = excluded.reservation_ids_json,
                position_id    = excluded.position_id,
                protection_id  = excluded.protection_id,
                intent_id      = excluded.intent_id,
                plan_id        = excluded.plan_id,
                updated_at     = excluded.updated_at,
                terminal_at    = excluded.terminal_at
            """,
            (
                run.executor_id, run.kind, run.account_id, run.strategy_id, run.market,
                run.state, run.close_type, run.retries, run.last_heartbeat,
                json.dumps(run.plan_json), json.dumps(run.config_json),
                json.dumps(run.result_json),
                json.dumps(run.order_ids), json.dumps(run.reservation_ids),
                run.position_id, run.protection_id, run.intent_id, run.plan_id,
                run.created_at, run.updated_at, run.terminal_at,
            ),
        )

    def _load(self, executor_id: str) -> ExecutorRun | None:
        row = self._con_lazy().execute(
            "SELECT * FROM executor_runs WHERE executor_id = ?", (executor_id,),
        ).fetchone()
        if not row:
            return None
        return _row_to_run(row)

    def list_active(self, *, account_id: str | None = None) -> list[ExecutorRun]:
        sql = "SELECT * FROM executor_runs WHERE state NOT IN ('canceled','done','failed','rejected')"
        params: tuple[Any, ...] = ()
        if account_id:
            sql += " AND account_id = ?"
            params = (account_id,)
        rows = self._con_lazy().execute(sql, params).fetchall()
        return [_row_to_run(r) for r in rows]

    def list_recent(self, *, limit: int = 50) -> list[ExecutorRun]:
        rows = self._con_lazy().execute(
            "SELECT * FROM executor_runs ORDER BY updated_at DESC LIMIT ?", (int(limit),),
        ).fetchall()
        return [_row_to_run(r) for r in rows]

    # -- creation ---------------------------------------------------------------
    def create_market_order(
        self,
        *,
        candidate: OrderCandidate,
        intent_id: str | None = None,
        plan_id: str | None = None,
        protection: ProtectionRule | None = None,
    ) -> Executor:
        from .market_order import MarketOrderConfig, MarketOrderExecutor

        cfg = MarketOrderConfig(
            kind="market_order",
            account_id=candidate.account_id,
            strategy_id=candidate.strategy_id,
            market=candidate.market,
            candidate=candidate.asdict(),
            protection=(protection.asdict() if protection else None),
        )
        plan: dict[str, Any] = {"candidate": candidate.asdict()}
        if protection is not None:
            plan["protection"] = protection.asdict()
        executor = MarketOrderExecutor.new(
            account_id=candidate.account_id,
            strategy_id=candidate.strategy_id,
            market=candidate.market,
            config=cfg,
            plan=plan,
            intent_id=intent_id,
            plan_id=plan_id,
            paths=self.paths,
        )
        self._persist(executor.run)
        return executor

    def create_position_protection(
        self,
        *,
        rule: ProtectionRule,
        position_id: str,
    ) -> Executor:
        from .position_protection import (
            PositionProtectionExecutor,
            ProtectionExecutorConfig,
        )

        cfg = ProtectionExecutorConfig(
            kind="position_protection",
            account_id=rule.account_id,
            strategy_id=rule.strategy_id,
            market=rule.market,
            rule=rule.asdict(),
            position_id=position_id,
        )
        executor = PositionProtectionExecutor.new(
            account_id=rule.account_id,
            strategy_id=rule.strategy_id,
            market=rule.market,
            config=cfg,
            plan={"rule": rule.asdict()},
            position_id=position_id,
            protection_id=rule.protection_id,
            paths=self.paths,
        )
        self._persist(executor.run)
        return executor

    # -- driving ---------------------------------------------------------------
    def step_executor(self, executor: Executor) -> bool:
        """Drive a single tick of one executor. Returns True if terminal."""
        try:
            executor.heartbeat()
            if executor.run.state == "created":
                executor.transition("reserving")
                executor.prepare()
                if executor.run.state == "reserving":
                    executor.transition("ready")
            terminal = executor.step()
        except Exception as exc:  # pragma: no cover — defensive
            log.exception("executor %s step failed", executor.run.executor_id)
            executor.run.retries += 1
            executor.transition("failed", close_type="failed")
            executor.store_result({"error": str(exc)})
            terminal = True
        finally:
            self._persist(executor.run)
        return terminal

    def run_until_terminal(
        self,
        executor: Executor,
        *,
        max_steps: int | None = None,
    ) -> ExecutorRun:
        cap = int(max_steps if max_steps is not None else self.max_steps_per_tick)
        for _ in range(cap):
            if self.step_executor(executor):
                break
        return executor.run

    def run_once(self) -> int:
        """Tick every active executor once. Returns the count touched.

        On restart, this resumes any non-terminal executor without
        reissuing orders — subclasses are responsible for inspecting
        ``run.order_ids`` and the durable :class:`OrderTracker` state
        to decide what's already in flight.
        """
        registry = _registry()
        touched = 0
        for run in self.list_active():
            cls = registry.get(run.kind)
            if cls is None:
                continue
            executor = cls(run, self.paths)
            self.step_executor(executor)
            touched += 1
        return touched

    # -- operator hooks --------------------------------------------------------
    def cancel(self, executor_id: str, *, reason: str = "manual_cancel") -> ExecutorRun | None:
        run = self._load(executor_id)
        if run is None or run.is_terminal:
            return run
        registry = _registry()
        cls = registry.get(run.kind)
        if cls is None:
            return run
        executor = cls(run, self.paths)
        executor.transition("canceling", close_type=reason)  # type: ignore[arg-type]
        try:
            executor.on_cancel()
        finally:
            if not executor.run.is_terminal:
                executor.transition("canceled", close_type=reason)  # type: ignore[arg-type]
            self._persist(executor.run)
        return executor.run

    def get(self, executor_id: str) -> ExecutorRun | None:
        return self._load(executor_id)


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _row_to_run(row: Any) -> ExecutorRun:
    return ExecutorRun(
        executor_id=str(row["executor_id"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        account_id=str(row["account_id"]),
        strategy_id=str(row["strategy_id"]),
        market=str(row["market"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        close_type=str(row["close_type"] or ""),  # type: ignore[arg-type]
        retries=int(row["retries"] or 0),
        last_heartbeat=(float(row["last_heartbeat"]) if row["last_heartbeat"] is not None else None),
        plan_json=json.loads(str(row["plan_json"] or "{}")),
        config_json=json.loads(str(row["config_json"] or "{}")),
        result_json=json.loads(str(row["result_json"] or "{}")),
        order_ids=list(json.loads(str(row["order_ids_json"] or "[]"))),
        reservation_ids=list(json.loads(str(row["reservation_ids_json"] or "[]"))),
        position_id=(row["position_id"] or None),
        protection_id=(row["protection_id"] or None),
        intent_id=(row["intent_id"] or None),
        plan_id=(row["plan_id"] or None),
        created_at=float(row["created_at"] or 0.0),
        updated_at=float(row["updated_at"] or 0.0),
        terminal_at=(float(row["terminal_at"]) if row["terminal_at"] is not None else None),
    )


__all__ = ["ExecutorOrchestrator"]
