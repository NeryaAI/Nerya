"""TriggerRuntime — the top-level object that the agent kernel, cron and SDK invoke."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.config import Config
from .event import TriggerEvent
from .router import RouterResult, TriggerRouter


STRATEGY_RUN_TICK_TARGET = "skill:strategy.run_tick"
STRATEGY_RUN_TUNING_TARGET = "skill:strategy.run_tuning"


@dataclass
class StrategyTriggerExecutionResult:
    event_id: str
    target: str
    status: str
    strategy_id: str | None
    route_id: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    def asdict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "target": self.target,
            "status": self.status,
            "strategy_id": self.strategy_id,
            "route_id": self.route_id,
            "result": dict(self.result or {}),
            "error": self.error,
        }


@dataclass
class TriggerRuntime:
    config: Config
    router: TriggerRouter
    agent_task_executor_factory: Any = None

    @classmethod
    def boot(cls, config: Config) -> "TriggerRuntime":
        return cls(config=config, router=TriggerRouter(config))

    def emit(self, event: TriggerEvent) -> Any:
        if event.dry_run:
            return self.router.dry_run(event)
        routed = self.router.route(event)
        if (
            routed.status == "routed"
            and (routed.target or event.target) == "skill:strategy.agent_task"
        ):
            return self._agent_task_executor().execute(event, routed)
        if (
            routed.status == "routed"
            and (routed.target or event.target)
            in {STRATEGY_RUN_TICK_TARGET, STRATEGY_RUN_TUNING_TARGET}
        ):
            return self._execute_strategy_target(event, routed)
        return routed

    def explain(self, event: TriggerEvent) -> dict[str, Any]:
        """Return a static route-decision trace for ``event``."""
        return self.router.explain(event)

    def replay(self, event: TriggerEvent, *,
               reason: str = "operator_replay") -> Any:
        """Re-route a historical event, bypassing idempotency dedupe."""
        routed = self.router.replay(event, reason=reason)
        if (
            routed.status == "routed"
            and (routed.target or event.target) == "skill:strategy.agent_task"
        ):
            return self._agent_task_executor().execute(event, routed)
        if (
            routed.status == "routed"
            and (routed.target or event.target)
            in {STRATEGY_RUN_TICK_TARGET, STRATEGY_RUN_TUNING_TARGET}
        ):
            return self._execute_strategy_target(event, routed)
        return routed

    def _agent_task_executor(self):
        if self.agent_task_executor_factory is not None:
            return self.agent_task_executor_factory(self.config)
        from .strategy_agent_task_executor import StrategyAgentTaskExecutor
        return StrategyAgentTaskExecutor(config=self.config)

    def _execute_strategy_target(
        self,
        event: TriggerEvent,
        route_result: RouterResult,
    ) -> StrategyTriggerExecutionResult:
        target = route_result.target or event.target
        payload = dict(event.payload or {})
        strategy_id = (
            route_result.strategy_id
            or event.strategy_id
            or payload.get("strategy_id")
        )
        if not strategy_id:
            return StrategyTriggerExecutionResult(
                event_id=event.event_id,
                target=target or "",
                status="failed",
                strategy_id=None,
                route_id=route_result.route_id,
                error={
                    "code": "strategy_id_required",
                    "message": f"strategy_id is required for {target}",
                },
            )
        try:
            if target == STRATEGY_RUN_TICK_TARGET:
                result = self._run_strategy_tick(str(strategy_id), event, payload)
            elif target == STRATEGY_RUN_TUNING_TARGET:
                result = self._run_strategy_tuning(str(strategy_id), event, payload)
            else:
                return StrategyTriggerExecutionResult(
                    event_id=event.event_id,
                    target=target or "",
                    status="routed_only",
                    strategy_id=strategy_id,
                    route_id=route_result.route_id,
                )
        except Exception as exc:
            return StrategyTriggerExecutionResult(
                event_id=event.event_id,
                target=target or "",
                status="failed",
                strategy_id=str(strategy_id),
                route_id=route_result.route_id,
                error={
                    "code": "strategy_target_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
        return StrategyTriggerExecutionResult(
            event_id=event.event_id,
            target=target or "",
            status="executed",
            strategy_id=str(strategy_id),
            route_id=route_result.route_id,
            result=result,
        )

    def _run_strategy_tick(
        self,
        strategy_id: str,
        event: TriggerEvent,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        from ..skills.kernel import SkillKernel
        from ..sdk.strategy_api import StrategyAPI

        mode = payload.get("mode_override") or payload.get("mode")
        note = (
            payload.get("note")
            or payload.get("manual_reason")
            or payload.get("reason")
            or "trigger"
        )
        api = StrategyAPI(
            config=self.config,
            skills=SkillKernel.boot(self.config),
        )
        record = api.run_tick(
            strategy_id,
            trigger_payload=payload,
            trigger_event_id=event.event_id,
            operator=str(payload.get("operator") or event.source or ""),
            note=str(note),
            mode_override=str(mode).strip().lower() if mode else None,
        )
        return dict(record) if isinstance(record, dict) else record.asdict()

    def _run_strategy_tuning(
        self,
        strategy_id: str,
        event: TriggerEvent,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        from ..skills.kernel import SkillKernel
        from ..sdk.strategy_api import StrategyAPI

        note = (
            payload.get("note")
            or payload.get("manual_reason")
            or payload.get("reason")
            or "trigger"
        )
        api = StrategyAPI(
            config=self.config,
            skills=SkillKernel.boot(self.config),
        )
        return api.tuning.run(
            strategy_id,
            dry_run=bool(payload.get("dry_run", False)),
            operator=str(payload.get("operator") or event.source or ""),
            note=str(note),
            trigger_event_id=event.event_id,
        )

    def from_payload(self, payload: dict[str, Any]) -> TriggerEvent:
        # make sure payload has an event_id
        payload = dict(payload)
        payload.setdefault("event_id", None)
        if not payload.get("event_id"):
            from ..core.ids import event_id
            payload["event_id"] = event_id()
        return TriggerEvent(**payload)
