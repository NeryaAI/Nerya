"""Non-blocking producer adapters; execution stays in CommandRuntime."""
from dataclasses import replace
from types import SimpleNamespace
import time

from ..agent.task_runs import task_runs
from .task_execution import schedule_spec,strategy_spec


class ManagedScheduledRunner:
    def __init__(self,config):self.config=config

    def run_many(self,entry,*,now_ts=None,occurrence=None):
        from ..triggers.scheduled_session import ScheduledSessionRunner
        now_ts=time.time() if now_ts is None else now_ts
        return [self.run_once(entry,now_ts=now_ts,session_id=sid,occurrence=occurrence)
                for sid in ScheduledSessionRunner._session_ids_for(entry,now_ts)]

    def run_once(self,entry,*,now_ts=None,session_id=None,occurrence=None,manual_id=None,actor=None):
        from ..triggers.scheduled_session import ScheduledSessionResult
        now_ts=time.time() if now_ts is None else now_ts
        due=(occurrence or {}).get("scheduled_at",now_ts)
        spec=schedule_spec(self.config,entry,now_ts=due,session_id=session_id)
        if occurrence:spec=replace(spec,snapshot={**spec.snapshot,"occurrence":occurrence,'admission_expires_at':due+300})
        trigger_id=manual_id or f"schedule:{entry.id}:{due:.6f}"
        receipt=task_runs(self.config).admit(spec,actor=actor or entry.owner_actor_id,
            trigger={"schedule_id":entry.id,"scheduled_at":due},trigger_id=trigger_id,
            trigger_kind="manual" if manual_id else "schedule",scheduled_at=None if manual_id else due,
            client_request_id=manual_id,skip_reason="missed_window" if (occurrence or {}).get("missed") else None)
        result=ScheduledSessionResult(entry.id,receipt["session_id"],receipt["admission_status"]=="admitted",
            turn_id=receipt.get("turn_id"),trigger_event_id=trigger_id,execution_status=receipt["execution_status"],
            run_id=receipt["run_id"],command_id=receipt.get("command_id"))
        return result


def admit_strategy(config,event,route,*,prepared_task=None,prepared_inputs=None,expected_hash=None,actor_id=None,**unused):
    from ..triggers.strategy_agent_task_executor import StrategyAgentTaskExecutionResult
    spec=strategy_spec(config,event,route,prepared_task=prepared_task,prepared_inputs=prepared_inputs)
    if expected_hash and expected_hash!=spec.source_revision:
        from ..agent.command_store import CommandError
        raise CommandError("strategy_version_changed")
    receipt=task_runs(config).admit(spec,actor=str(actor_id or config.get("runtime.task_actor_id") or "local:loopback"),
        trigger=event.asdict(),trigger_id=event.idempotency_key or event.event_id,trigger_kind="event")
    return StrategyAgentTaskExecutionResult(event.event_id,event.target,receipt["execution_status"],spec.strategy_id,
        task_id=receipt["run_id"],session_id=receipt["session_id"],turn_id=receipt.get("turn_id"),
        route_id=route.route_id,result=receipt,run_id=receipt["run_id"],command_id=receipt.get("command_id"))
