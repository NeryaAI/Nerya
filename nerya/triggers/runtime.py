"""TriggerRuntime — the top-level object that the agent kernel, cron and SDK invoke."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.config import Config
from .event import TriggerEvent
from .router import RouterResult, TriggerRouter


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
        return routed

    def _agent_task_executor(self):
        if self.agent_task_executor_factory is not None:
            return self.agent_task_executor_factory(self.config)
        from .strategy_agent_task_executor import StrategyAgentTaskExecutor
        return StrategyAgentTaskExecutor(config=self.config)

    def from_payload(self, payload: dict[str, Any]) -> TriggerEvent:
        # make sure payload has an event_id
        payload = dict(payload)
        payload.setdefault("event_id", None)
        if not payload.get("event_id"):
            from ..core.ids import event_id
            payload["event_id"] = event_id()
        return TriggerEvent(**payload)
