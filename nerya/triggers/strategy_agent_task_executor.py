"""Execute ``skill:strategy.agent_task`` trigger targets."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core import jsonl
from ..core.atomic_write import atomic_write_text
from ..core.config import Config
from ..core.errors import NeryaError
from ..core.time import now_iso
from ..strategies.agent_task import StrategyAgentTask
from ..strategies.agent_task_mode import (
    AGENT_TASK_TARGET,
    agent_task_requested,
    agent_team_roles,
)
from ..strategies.context import build_strategy_context
from ..strategies.package import StrategyPackage, load_package
from ..strategies.runner import _run_with_timeout
from ..strategy_history import store as history_store
from ..tools.types import ToolCall
from .event import TriggerEvent
from .router import RouterResult


TARGET = AGENT_TASK_TARGET


@dataclass
class StrategyAgentTaskExecutionResult:
    event_id: str
    target: str
    status: str
    strategy_id: str | None
    task_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    route_id: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    def asdict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "target": self.target,
            "status": self.status,
            "strategy_id": self.strategy_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "route_id": self.route_id,
            "result": dict(self.result or {}),
            "error": self.error,
        }


@dataclass
class StrategyAgentTaskExecutor:
    config: Config
    skills: Any = None
    kernel_factory: Any = None
    connector_registry: Any = None

    def execute(
        self,
        event: TriggerEvent,
        route_result: RouterResult,
        *,
        prepared_task: Any = None,
        prepared_inputs: dict[str, Any] | None = None,
        expected_hash: str = "",
        cancel_token: Any = None,
    ) -> StrategyAgentTaskExecutionResult:
        target = route_result.target or event.target
        strategy_id = (
            route_result.strategy_id
            or event.strategy_id
            or (event.payload or {}).get("strategy_id")
        )
        task_id = "agent_task_" + uuid.uuid4().hex[:12]
        if target != TARGET:
            return StrategyAgentTaskExecutionResult(
                event_id=event.event_id,
                target=target or "",
                status="routed_only",
                strategy_id=strategy_id,
                route_id=route_result.route_id,
            )
        if route_result.status != "routed":
            return StrategyAgentTaskExecutionResult(
                event_id=event.event_id,
                target=target,
                status=route_result.status,
                strategy_id=strategy_id,
                route_id=route_result.route_id,
                error={"code": "not_routed", "reason": route_result.reason},
            )
        if not strategy_id:
            return self._failed(
                event,
                target=target,
                strategy_id=None,
                task_id=task_id,
                code="strategy_id_required",
                message="strategy_id is required for skill:strategy.agent_task",
                route_id=route_result.route_id,
            )

        try:
            package = load_package(self.config.paths, str(strategy_id))
            self._assert_mode_allowed(package)
            from ..strategies.continuous_config import is_continuous
            if is_continuous(package.manifest):
                from ..strategies.continuous import assert_event_active
                if prepared_task is None:
                    raise NeryaError("continuous listeners require a prepared event, not another tick")
                assert_event_active(self.config, package.strategy_id, event.event_id)
            if expected_hash and package.content_hash != expected_hash:
                raise NeryaError("strategy source changed before event execution")
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            from ..strategies.agent_execution import execution_config
            from ..strategies.input_context import context_prompt
            scoped_config = execution_config(self.config, package.manifest)
            if prepared_task is None:
                task = self._build_task(package, event, task_id)
            else:
                ctx = build_strategy_context(config=scoped_config, package=package, skills=self.skills,
                    run_id=task_id, connector_registry=self.connector_registry, trigger_event=event)
                from ..strategies.input_context import safe_data
                ctx.inputs._values = safe_data(prepared_inputs or {})
                task = self._call_task_builder(package, lambda _: prepared_task, ctx)
            if task.status == "dispatch" and task.prompt.strip():
                task.prompt = context_prompt(task, package, task_id)
        except Exception as exc:
            return self._failed(
                event,
                target=target,
                strategy_id=strategy_id,
                task_id=task_id,
                code="task_build_failed",
                message=f"{type(exc).__name__}: {exc}",
                route_id=route_result.route_id,
                trace=traceback.format_exc(limit=8),
            )

        if task.status in {"skip", "error"}:
            status = "skipped" if task.status == "skip" else "failed"
            row = self._task_row(
                event=event,
                package=package,
                task=task,
                task_id=task_id,
                session_id=None,
                turn_id=None,
                status=status,
                prompt_artifact=None,
            )
            self._record_task(package.strategy_id, None, row)
            return StrategyAgentTaskExecutionResult(
                event_id=event.event_id,
                target=target,
                status=status,
                strategy_id=package.strategy_id,
                task_id=task_id,
                route_id=route_result.route_id,
                result=row,
                error=(
                    {"code": "strategy_agent_task_error", "message": task.reason}
                    if task.status == "error"
                    else None
                ),
            )
        if not task.prompt.strip():
            return self._failed(
                event,
                target=target,
                strategy_id=package.strategy_id,
                task_id=task_id,
                code="empty_prompt",
                message="StrategyAgentTask.dispatch requires a non-empty prompt",
                route_id=route_result.route_id,
            )

        session_key = self._resolve_session_key(package, event, task, task_id)
        policy = package.manifest.agent_session.policy or "per_strategy_market_timeframe"
        session_profile = self._session_profile_module()
        session_id = session_profile.strategy_agent_session_id(
            strategy_id=package.strategy_id,
            session_key=session_key,
            policy=policy,
        )
        profile = self._profile_for(package)
        profile_record = session_profile.ensure_strategy_agent_profile(
            paths=self.config.paths,
            session_id=session_id,
            strategy_id=package.strategy_id,
            profile=profile,
            session_key=session_key,
            policy=policy,
        )
        required_team_run = None
        task.metadata["execution_policy"] = {
            "max_iterations": scoped_config.get("agent.native.max_iterations"),
            "max_tool_calls": scoped_config.get("agent.native.max_total_tool_calls"),
            "max_wall_seconds": scoped_config.get("agent.native.max_wall_seconds"),
            "capabilities": profile.get("capability_mode", "inherit"),
        }
        prompt_artifact = self._write_prompt_artifacts(
            package=package,
            task_id=task_id,
            task=task,
            session_id=session_id,
            profile_record=profile_record,
        )

        trigger_for_agent = {
            "id": event.event_id,
            "event_id": event.event_id,
            "source": "strategy",
            "kind": "strategy.agent_task",
            "target": TARGET,
            "strategy_id": package.strategy_id,
            "strategy_triggered": True,
            "payload": {
                "text": task.prompt,
                "metadata": dict(task.metadata or {}),
                "artifacts": list(task.artifacts or []),
                "session_id": session_id,
                "task_id": task_id,
                "strategy_agent_task": True,
                "trigger_event_id": event.event_id,
            },
        }
        started_at = now_iso()
        started_clock = time.monotonic()
        execution_turn_id = "turn_" + uuid.uuid4().hex
        running = self._task_row(
            event=event, package=package, task=task, task_id=task_id,
            session_id=session_id, turn_id=execution_turn_id, status="running",
            prompt_artifact=prompt_artifact,
        )
        running["started_at"] = started_at
        self._record_task(package.strategy_id, session_id, running)
        try:
            kernel = self._kernel(scoped_config)
            required_team_run = self._run_required_team(
                package=package, event=event, task=task, task_id=task_id,
                session_id=session_id, profile=profile, kernel=kernel,
                execution_turn_id=execution_turn_id, config=scoped_config,
            )
            if required_team_run is not None:
                task.prompt = self._required_team_decision_prompt(package=package, task=task, team_run=required_team_run)
                task.metadata["required_team_run_id"] = required_team_run.get("team_run_id")
                task.metadata["required_team_run_ok"] = bool(required_team_run.get("ok"))
                self._write_prompt_artifacts(package=package, task_id=task_id, task=task,
                    session_id=session_id, profile_record=profile_record)
                trigger_for_agent["payload"].update(text=task.prompt, metadata=dict(task.metadata), artifacts=list(task.artifacts))
                wall = float(scoped_config.get("agent.native.max_wall_seconds", 0) or 0)
                if wall > 0:
                    remaining = wall - (time.monotonic() - started_clock)
                    if remaining <= 0:
                        raise TimeoutError("Parallel team exhausted this run's wall-clock budget")
                    scoped_config.data["agent"]["native"]["max_wall_seconds"] = remaining
            turn_result = kernel.run_turn(
                trigger=trigger_for_agent,
                strategy_id=package.strategy_id,
                session_id=session_id,
                turn_id=execution_turn_id,
                attached_skills=self._attached_skills(task, profile),
                **({"cancel_token": cancel_token} if cancel_token is not None else {}),
            )
        except Exception as exc:
            return self._failed(
                event, target=target, strategy_id=package.strategy_id,
                task_id=task_id, session_id=session_id, turn_id=execution_turn_id,
                code="agent_execution_failed", message=f"{type(exc).__name__}: {exc}",
                route_id=route_result.route_id, trace=traceback.format_exc(limit=8),
                started_at=started_at,
                duration_ms=round((time.monotonic() - started_clock) * 1000),
            )
        turn_id = getattr(turn_result, "turn_id", None)
        row = self._task_row(
            event=event,
            package=package,
            task=task,
            task_id=task_id,
            session_id=session_id,
            turn_id=turn_id,
            status="executed",
            prompt_artifact=prompt_artifact,
        )
        preflight_actions = []
        preflight_trace = []
        if required_team_run is not None:
            preflight_actions.append(required_team_run["action"])
            preflight_trace.append(required_team_run["trace"])
            row["required_team_run"] = required_team_run["summary"]
        row["actions"] = preflight_actions + list(getattr(turn_result, "actions", []) or [])
        row["tool_trace"] = preflight_trace + list(getattr(turn_result, "tool_trace", []) or [])
        row["decision"] = getattr(turn_result, "decision", None)
        row["stopped_reason"] = getattr(turn_result, "stopped_reason", None)
        row["started_at"] = started_at
        row["finished_at"] = now_iso()
        row["duration_ms"] = round((time.monotonic() - started_clock) * 1000)
        row["final_text"] = getattr(turn_result, "final_text", "")
        row["iterations"] = getattr(turn_result, "iterations", None)
        row["budget"] = dict(getattr(turn_result, "budget", {}) or {})
        # Executed means the kernel returned, not a successful business outcome.
        # Consumers must retain stopped_reason and the actual final response.
        self._record_task(package.strategy_id, session_id, row)
        jsonl.append(self.config.paths.journal("triggers"), {
            "kind": "trigger.agent_task_executed",
            "ts": now_iso(),
            "event_id": event.event_id,
            "target": target,
            "strategy_id": package.strategy_id,
            "task_id": task_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "route_id": route_result.route_id,
        })
        return StrategyAgentTaskExecutionResult(
            event_id=event.event_id,
            target=target,
            status="executed",
            strategy_id=package.strategy_id,
            task_id=task_id,
            session_id=session_id,
            turn_id=turn_id,
            route_id=route_result.route_id,
            result=row,
        )

    def _assert_mode_allowed(self, package: StrategyPackage) -> None:
        if package.manifest.mode == "live" and not self.config.live_trading_enabled():
            raise NeryaError(
                "live strategy agent task requires runtime.live_trading_enabled=true"
            )

    def _build_task(
        self,
        package: StrategyPackage,
        event: TriggerEvent,
        task_id: str,
    ) -> StrategyAgentTask:
        from ..strategies.agent_execution import execution_config
        ctx = build_strategy_context(
            config=execution_config(self.config, package.manifest),
            package=package,
            skills=self.skills,
            run_id=task_id,
            session_id=None,
            connector_registry=self.connector_registry,
            trigger_event=event,
        )
        module = self._load_strategy_module(package)
        fn = getattr(module, "build_agent_task", None)
        if callable(fn):
            raw = self._call_task_builder(package, fn, ctx)
            return StrategyAgentTask.from_value(raw)
        fn = getattr(module, package.manifest.entrypoint_func, None)
        if not callable(fn):
            raise AttributeError(
                f"strategy {package.strategy_id!r}: missing build_agent_task(ctx)"
            )
        raw = self._call_task_builder(package, fn, ctx)
        return StrategyAgentTask.from_value(raw)

    def _call_task_builder(self, package: StrategyPackage, fn: Any, ctx: Any) -> Any:
        def _call() -> Any:
            from ..strategies.input_context import collect_task_context
            task = StrategyAgentTask.from_value(fn(ctx))
            if task.status == "dispatch" and task.roles is not None:
                if any(role not in package.manifest.subagents for role in task.roles):
                    raise ValueError("Task roles must refer to declared strategy subagents")
                task.metadata["selected_roles"] = list(task.roles)
            collect_task_context(task, ctx, package.manifest.extras.get("agent_context", {}))
            return task

        return _run_with_timeout(
            _call,
            seconds=float(package.manifest.policy.max_run_seconds or 0),
        )

    def _load_strategy_module(self, package: StrategyPackage):
        module_path = package.root / package.manifest.entrypoint_module
        if not module_path.exists():
            raise FileNotFoundError(str(module_path))
        suffix = uuid.uuid4().hex[:8]
        module_name = (
            f"_nerya_strategy_agent_task."
            f"{package.strategy_id}.{package.content_hash[:8]}_{suffix}"
        )
        spec = importlib.util.spec_from_file_location(
            module_name,
            module_path,
            submodule_search_locations=[str(package.root)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build spec for {module_path}")
        module = importlib.util.module_from_spec(spec)
        added_path = str(package.root)
        inserted = added_path not in sys.path
        if inserted:
            sys.path.insert(0, added_path)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            if inserted:
                try:
                    sys.path.remove(added_path)
                except ValueError:
                    pass
            sys.modules.pop(module_name, None)
        return module

    def _run_required_team(
        self,
        *,
        package: StrategyPackage,
        event: TriggerEvent,
        task: StrategyAgentTask,
        task_id: str,
        session_id: str,
        profile: dict[str, Any],
        kernel: Any,
        execution_turn_id: str = "",
        config: Config | None = None,
    ) -> dict[str, Any] | None:
        if not self._is_agent_team_task(task, package):
            return None
        roles = self._team_role_entries(package, task)
        if not roles:
            return None

        from ..skills.kernel import SkillKernel
        from ..subagents.dispatcher import SubAgentDispatcher
        effective = config or self.config
        team_config = package.manifest.extras.get("agent_execution", {}).get("team", {})

        skills = self.skills or getattr(kernel, "skills", None)
        if skills is None:
            skills = SkillKernel.boot(self.config)
        shared_payload = self._team_context_payload(package, event, task, task_id)
        call_id = f"toolu_required_team_{uuid.uuid4().hex[:12]}"
        call = ToolCall(
            name="team_run",
            id=call_id,
            caller="strategy.agent_task.required_team",
            turn_id=execution_turn_id,
            arguments={
                "team_run_id": f"team-{task_id[-12:]}",
                "task": self._team_mission(package, task),
                "roles": roles,
                "shared_payload": shared_payload,
                "max_parallel": min(len(roles), int(team_config.get("max_parallel") or effective.get("agent.subagents.max_parallel", 4) or 4)),
                "strategy_id": package.strategy_id,
                "session_id": session_id,
                "trigger_event_id": event.event_id,
            },
            metadata={
                "strategy_id": package.strategy_id,
                "session_id": session_id,
                "trigger_event_id": event.event_id,
                "required_by": "strategy.agent_team",
                "turn_deadline_epoch": time.time() + float(effective.get("agent.native.max_wall_seconds", 1800)),
                "wall_time_final_synthesis_seconds": float(effective.get("agent.native.wall_time_final_synthesis_seconds", 30)),
                "task_id": task_id,
            },
        )
        scope = SubAgentDispatcher.for_workspace(effective, skills,
            strategy_id=package.strategy_id, session_id=session_id, turn_id=execution_turn_id)
        result = scope.executor.execute(call)
        data = self._tool_json_data(result)
        team_run_id = str(
            (data or {}).get("team_run_id")
            or call.arguments.get("team_run_id")
            or ""
        )
        ok = not bool(result.is_error) and (data or {}).get("ok") is not False and not bool((data or {}).get("roles_failed"))
        from ..strategies.input_context import safe_data
        full_data = safe_data({k: v for k, v in (data or {}).items() if k not in {"results", "failures", "steps", "_transcript", "transcript", "thinking"}})
        for group in ("results", "failures"):
            full_data[group] = [safe_data({k: v for k, v in row.items() if k not in {"steps", "_transcript", "transcript", "thinking", "prompt"}})
                for row in (data or {}).get(group, []) if isinstance(row, dict)]
        full_data["evidence_scope"] = "Actual team tool results. Individual long outputs may be compacted by the team tool; inspect the original team records for full evidence."
        artifact = package.root / "agent_tasks" / task_id / "team-result.json"
        if not artifact.resolve().is_relative_to(package.root.resolve()):
            raise ValueError("team evidence path outside strategy root")
        atomic_write_text(artifact, json.dumps(full_data, ensure_ascii=False, indent=2))
        task.artifacts.append({"kind": "team_result", "path": artifact.relative_to(package.root).as_posix()})
        compact_data = self._compact_team_result(full_data)
        compact_call = {
            "name": call.name,
            "id": call.id,
            "caller": call.caller,
            "arguments": {
                "team_run_id": team_run_id,
                "task": call.arguments.get("task"),
                "roles": [
                    str((role or {}).get("name") or "")
                    for role in list(call.arguments.get("roles") or [])
                    if isinstance(role, dict)
                ],
                "strategy_id": package.strategy_id,
                "session_id": session_id,
                "trigger_event_id": event.event_id,
                "shared_payload": self._compact_jsonable(shared_payload, max_text=600),
            },
            "metadata": dict(call.metadata or {}),
        }
        compact_result = {
            "is_error": bool(result.is_error),
            "error": result.error.asdict() if result.error else None,
            "data": compact_data,
        }
        summary = {
            "team_run_id": team_run_id,
            "ok": ok,
            "roles_succeeded": list((data or {}).get("roles_succeeded") or []),
            "roles_failed": list((data or {}).get("roles_failed") or []),
            "is_error": bool(result.is_error),
            "error": result.error.asdict() if result.error else None,
        }
        return {
            "team_run_id": team_run_id,
            "ok": ok,
            "summary": summary,
            "data": compact_data,
            "shared_payload": shared_payload,
            "full_data": full_data,
            "artifact_path": f"strategies/{package.strategy_id}/{artifact.relative_to(package.root).as_posix()}",
            "action": {
                "action": "team_run",
                "skill_id": "native",
                "ok": ok,
                "forced_by": "strategy_agent_task_executor",
                "team_run_id": team_run_id,
                "result": compact_data,
            },
            "trace": {
                "action": "team_run",
                "call": compact_call,
                "result": compact_result,
                "forced_by": "strategy_agent_task_executor",
            },
        }

    @staticmethod
    def _is_agent_team_task(task: StrategyAgentTask, package: StrategyPackage | None = None) -> bool:
        if task.roles is not None:
            return bool(task.roles)
        if package is not None:
            team = package.manifest.extras.get("agent_execution", {}).get("team", {})
            if "enabled" in team:
                return team["enabled"]
            if package.manifest.extras.get("agent_task", {}).get("mode") == "agent_team":
                return True
        return (task.metadata or {}).get("execution_mode") == "agent_team"

    def _team_role_entries(
        self,
        package: StrategyPackage,
        task: StrategyAgentTask,
    ) -> list[dict[str, Any]]:
        meta = dict(task.metadata or {})
        team = package.manifest.extras.get("agent_execution", {}).get("team", {})
        raw_roles = task.roles if task.roles is not None else team.get("roles") or meta.get("roles") or agent_team_roles(package.manifest)
        roles = [str(r).strip() for r in (raw_roles or []) if str(r).strip()]
        from ..subagents.strategy_registry import StrategySubAgentRegistry
        registry = StrategySubAgentRegistry(paths=self.config.paths, strategy_id=package.strategy_id)
        entries = []
        for role in roles:
            entry = {"name": role, "instructions": self._role_task_for_markets(role, self._task_markets(package, task))}
            override = team.get("role_policies", {}).get(role)
            if override:
                spec = registry.get(role)
                # Keep the role's actual prompt and restrictions while applying
                # a reviewed per-role execution policy.
                policy = spec.execution_policy.merged(override)
                entry.update(prompt=spec.prompt, tier=spec.tier, allowed_skills=list(spec.allowed_skills),
                    execution_policy=policy.asdict())
            entries.append(entry)
        return entries

    @staticmethod
    def _role_task_for_markets(role: str, markets: list[str]) -> str:
        universe = ", ".join(markets) or "<markets>"
        return (
            f"Apply the assigned role {role!r} to the task and markets: {universe}. "
            "Use the role's instructions and collected evidence; distinguish facts from uncertainty."
        )

    def _team_context_payload(
        self, package: StrategyPackage, event: TriggerEvent,
        task: StrategyAgentTask, task_id: str,
    ) -> dict[str, Any]:
        from ..strategies.input_context import safe_data
        context = safe_data(task.context)
        cap = int(package.manifest.extras.get("agent_context", {}).get("max_chars", 64000))
        if len(json.dumps(context, ensure_ascii=False)) > cap:
            context = {"truncated": True, "artifact": task.metadata.get("input_context"),
                       "note": "Complete context is in the referenced artifact; the task contains a bounded preview."}
        return {
            "strategy_id": package.strategy_id, "task_id": task_id,
            "markets": self._task_markets(package, task),
            "account_id": self._task_account(package, task),
            "timeframe": self._task_timeframe(package, event, task),
            "workflow_context": context,
            "data_sources": safe_data(package.manifest.extras.get("data_sources", [])),
            "policy": package.manifest.policy.asdict(),
            "data_policy": "Use the supplied run snapshot. Missing data remains missing; acquire more evidence through authorized tools when useful.",
        }

    def _required_team_decision_prompt(
        self, *, package: StrategyPackage, task: StrategyAgentTask,
        team_run: dict[str, Any],
    ) -> str:
        from ..security.prompt_injection import wrap_untrusted
        data = team_run.get("full_data", team_run.get("data", {}))
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        cap = int(package.manifest.extras.get("agent_context", {}).get("max_chars", 64000))
        if len(text) > cap:
            text = json.dumps({"truncated": True, "artifact": team_run.get("artifact_path"),
                "original_chars": len(text), "preview": text[:max(0, cap - 600)]}, ensure_ascii=False)
        return "\n\n".join([
            task.prompt,
            "## Parallel team evidence\nThe configured team has returned. Complete the original task above, preserving its requested output and restrictions. "
            "Review each role's actual result and failures; gather more evidence, delegate follow-ups or iterate when useful. "
            "Do not repeat completed work merely to satisfy a template. A partial or failed role is not a successful result. "
            "All existing permission, account and trading boundaries remain in force.",
            wrap_untrusted("team_evidence", text),
        ])

    @staticmethod
    def _tool_json_data(result: Any) -> dict[str, Any] | None:
        for part in list(getattr(result, "content", []) or []):
            if getattr(part, "type", "") == "json" and isinstance(part.data, dict):
                return dict(part.data)
        return None

    @staticmethod
    def _compact_jsonable(value: Any, *, max_text: int = 1200) -> Any:
        if isinstance(value, dict):
            return {
                str(k): StrategyAgentTaskExecutor._compact_jsonable(v, max_text=max_text)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [
                StrategyAgentTaskExecutor._compact_jsonable(v, max_text=max_text)
                for v in value[:24]
            ]
        if isinstance(value, str) and len(value) > max_text:
            return value[:max_text] + "...[truncated]"
        return value

    @staticmethod
    def _compact_team_result(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {"raw": data}
        out = {
            "team_run_id": data.get("team_run_id"),
            "roles_requested": data.get("roles_requested"),
            "roles_succeeded": data.get("roles_succeeded"),
            "roles_failed": data.get("roles_failed"),
            "tokens_total": data.get("tokens_total"),
            "usd_total": data.get("usd_total"),
            "aggregated": data.get("aggregated"),
            "failures": data.get("failures"),
            "results": [],
        }
        for row in list(data.get("results") or [])[:12]:
            if not isinstance(row, dict):
                continue
            out["results"].append({
                "subagent": row.get("subagent"),
                "ok": row.get("ok"),
                "tier": row.get("tier"),
                "output": StrategyAgentTaskExecutor._compact_jsonable(row.get("output")),
                "error": row.get("error"),
                "error_kind": row.get("error_kind"),
            })
        return out

    @staticmethod
    def _task_markets(
        package: StrategyPackage,
        task: StrategyAgentTask,
    ) -> list[str]:
        meta = dict(task.metadata or {})
        raw = meta.get("markets") or task.session_key.get("markets")
        if isinstance(raw, str):
            markets = [m.strip() for m in raw.split(",") if m.strip()]
        elif isinstance(raw, list | tuple):
            markets = [str(m).strip() for m in raw if str(m).strip()]
        else:
            market = meta.get("market") or task.session_key.get("market")
            markets = [str(market).strip()] if market else []
        return markets or list(package.manifest.markets)

    @staticmethod
    def _task_timeframe(
        package: StrategyPackage,
        event: TriggerEvent,
        task: StrategyAgentTask,
    ) -> str:
        meta = dict(task.metadata or {})
        payload = dict(event.payload or {})
        return str(
            meta.get("timeframe")
            or task.session_key.get("timeframe")
            or payload.get("timeframe")
            or payload.get("interval")
            or next((str(source.get("timeframe")) for source in (package.manifest.extras.get("data_sources") or []) if isinstance(source, dict) and source.get("timeframe")), "")
        )

    @staticmethod
    def _task_account(package: StrategyPackage, task: StrategyAgentTask) -> str:
        meta = dict(task.metadata or {})
        return str(
            meta.get("account_id")
            or (package.manifest.accounts[0] if package.manifest.accounts else "")
        )

    def _team_mission(self, package: StrategyPackage, task: StrategyAgentTask) -> str:
        return task.prompt

    def _kernel(self, config: Config | None = None):
        effective = config or self.config
        if self.kernel_factory is not None:
            return self.kernel_factory(effective)
        import importlib
        from ..tools.permissions import PermissionMode
        agent_mod = importlib.import_module("nerya.agent.kernel")
        skills_mod = importlib.import_module("nerya.skills.kernel")
        skills = self.skills or skills_mod.SkillKernel.boot(effective)
        return agent_mod.AgentKernel(config=effective, skills=skills,
            permission_mode=PermissionMode(effective.get("runtime.permission_mode", "default")),
            llm_tier=effective.get("agent.native.tier"))

    def _resolve_session_key(
        self,
        package: StrategyPackage,
        event: TriggerEvent,
        task: StrategyAgentTask,
        task_id: str,
    ) -> dict[str, Any]:
        policy = package.manifest.agent_session.policy or "per_strategy_market_timeframe"
        meta = dict(task.metadata or {})
        payload = dict(event.payload or {})
        market = (
            task.session_key.get("market")
            or meta.get("market")
            or payload.get("market")
            or (package.manifest.markets[0] if package.manifest.markets else "")
        )
        timeframe = (
            task.session_key.get("timeframe")
            or meta.get("timeframe")
            or payload.get("timeframe")
            or ""
        )
        if policy == "per_signal":
            return {"event_id": event.event_id, "task_id": task_id}
        if policy == "per_strategy":
            return {}
        if policy == "per_strategy_market":
            return {"market": market}
        if policy == "custom":
            return dict(task.session_key or {})
        key = dict(task.session_key or {})
        if task.path:
            key.setdefault("path", task.path)
        key.setdefault("market", market)
        key.setdefault("timeframe", timeframe)
        return key

    def _profile_for(self, package: StrategyPackage) -> dict[str, Any]:
        profile = package.manifest.agent_profile.asdict()
        policy = package.manifest.policy
        use_agent_task = agent_task_requested(package.manifest)
        if not profile.get("title"):
            profile["title"] = f"{package.strategy_id} strategy agent"
        if not profile.get("role"):
            profile["role"] = (
                "Complete the strategy's task using supplied evidence and available tools. "
                "Research, plan, delegate and iterate when useful. Follow the requested "
                "output and all account, trading and operator restrictions."
            )
        if not profile.get("accounts"):
            profile["accounts"] = list(package.manifest.accounts)
        if not profile.get("markets"):
            profile["markets"] = list(package.manifest.markets)
        # An absent custom list inherits the main Agent's existing capability
        # surface. Explicit lists remain restrictive in the executor as well.
        profile["capability_mode"] = package.manifest.extras.get("agent_execution", {}).get(
            "capabilities", "custom" if profile.get("allowed_tools") else "inherit")
        # Explicit profiles are capability boundaries, not hints to broaden.
        # In particular, an observation Agent must not acquire trading/team
        # tools merely because it uses the Agent-task execution path.
        if use_agent_task:
            profile["order_rules"] = list(profile.get("order_rules") or [])
            if not profile.get("min_confidence_to_trade") and policy.min_confidence:
                profile["min_confidence_to_trade"] = policy.min_confidence
        risk_limits = dict(profile.get("risk_limits") or {})
        if policy.max_single_order_usd:
            risk_limits.setdefault("max_single_order_usd", policy.max_single_order_usd)
        if policy.max_daily_notional_usd:
            risk_limits.setdefault("max_daily_notional_usd", policy.max_daily_notional_usd)
        if policy.max_open_positions:
            risk_limits.setdefault("max_open_positions", policy.max_open_positions)
        if policy.min_confidence:
            risk_limits.setdefault("min_confidence", policy.min_confidence)
        profile["risk_limits"] = risk_limits
        return profile

    @staticmethod
    def _session_profile_module():
        import importlib

        return importlib.import_module("nerya.agent.session_profile")

    @staticmethod
    def _attached_skills(task: StrategyAgentTask, profile: dict[str, Any]) -> list[str] | None:
        out: list[str] = []
        for name in list(profile.get("attached_skills") or []) + list(task.attached_skills or []):
            s = str(name).strip()
            if s and s not in out:
                out.append(s)
        return out or None

    def _write_prompt_artifacts(
        self,
        *,
        package: StrategyPackage,
        task_id: str,
        task: StrategyAgentTask,
        session_id: str,
        profile_record: dict[str, Any],
    ) -> str:
        base = package.root / "agent_tasks" / task_id
        prompt_path = base / "prompt.md"
        metadata_path = base / "metadata.json"
        atomic_write_text(prompt_path, task.prompt)
        atomic_write_text(
            metadata_path,
            json.dumps(
                {
                    "task": task.asdict(),
                    "session_id": session_id,
                    "profile": profile_record,
                    "created_at": now_iso(),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
        )
        return str(prompt_path.relative_to(package.root)).replace("\\", "/")

    def _task_row(
        self,
        *,
        event: TriggerEvent,
        package: StrategyPackage,
        task: StrategyAgentTask,
        task_id: str,
        session_id: str | None,
        turn_id: str | None,
        status: str,
        prompt_artifact: str | None,
    ) -> dict[str, Any]:
        return {
            "kind": "strategy.agent_task",
            "ts": now_iso(),
            "package_hash": package.content_hash,
            "task_id": task_id,
            "strategy_id": package.strategy_id,
            "trigger_event_id": event.event_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "status": status,
            "reason": task.reason,
            "metadata": dict(task.metadata or {}),
            "artifacts": list(task.artifacts or []),
            "prompt_artifact": prompt_artifact,
            "prompt_chars": len(task.prompt or ""),
        }

    def _record_task(
        self,
        strategy_id: str,
        session_id: str | None,
        row: dict[str, Any],
    ) -> None:
        history_store.record_agent_task(
            self.config.paths,
            strategy_id=strategy_id,
            session_id=session_id,
            task=row,
        )
        jsonl.append(self.config.paths.journal("strategy_agent_tasks"), row)

    def _failed(
        self,
        event: TriggerEvent,
        *,
        target: str,
        strategy_id: str | None,
        task_id: str,
        code: str,
        message: str,
        route_id: str | None,
        trace: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        started_at: str | None = None,
        duration_ms: int | None = None,
    ) -> StrategyAgentTaskExecutionResult:
        error = {"code": code, "message": message}
        if trace:
            error["trace"] = trace
        if strategy_id:
            self._record_task(strategy_id, session_id, {
                "kind": "strategy.agent_task", "ts": now_iso(),
                "task_id": task_id, "strategy_id": strategy_id,
                "session_id": session_id, "turn_id": turn_id, "trigger_event_id": event.event_id,
                "status": "failed", "reason": message, "error": error,
                "started_at": started_at, "finished_at": now_iso(),
                "duration_ms": duration_ms,
            })
        jsonl.append(self.config.paths.journal("triggers"), {
            "kind": "trigger.agent_task_failed",
            "ts": now_iso(),
            "event_id": event.event_id,
            "target": target,
            "strategy_id": strategy_id,
            "task_id": task_id,
            "route_id": route_id,
            "error": error,
        })
        return StrategyAgentTaskExecutionResult(
            event_id=event.event_id,
            target=target,
            status="failed",
            strategy_id=strategy_id,
            task_id=task_id,
            route_id=route_id,
            error=error,
        )


__all__ = [
    "TARGET",
    "StrategyAgentTaskExecutionResult",
    "StrategyAgentTaskExecutor",
]
