"""Execute ``skill:strategy.agent_task`` trigger targets."""

from __future__ import annotations

import importlib.util
import json
import sys
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
from ..strategies.context import build_strategy_context
from ..strategies.package import StrategyPackage, load_package
from ..strategies.runner import _run_with_timeout
from ..strategy_history import store as history_store
from .event import TriggerEvent
from .router import RouterResult


TARGET = "skill:strategy.agent_task"


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
            task = self._build_task(package, event, task_id)
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
        kernel = self._kernel()
        turn_result = kernel.run_turn(
            trigger=trigger_for_agent,
            strategy_id=package.strategy_id,
            session_id=session_id,
            attached_skills=self._attached_skills(task, profile),
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
        row["actions"] = list(getattr(turn_result, "actions", []) or [])
        row["tool_trace"] = list(getattr(turn_result, "tool_trace", []) or [])
        row["decision"] = getattr(turn_result, "decision", None)
        row["stopped_reason"] = getattr(turn_result, "stopped_reason", None)
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
        ctx = build_strategy_context(
            config=self.config,
            package=package,
            skills=self.skills,
            run_id=task_id,
            session_id=None,
            connector_registry=self.connector_registry,
            trigger_event=event,
        )
        fn = self._load_task_callable(package)

        def _call() -> Any:
            return fn(ctx)

        raw = _run_with_timeout(
            _call,
            seconds=float(package.manifest.policy.max_run_seconds or 0),
        )
        return StrategyAgentTask.from_value(raw)

    def _load_task_callable(self, package: StrategyPackage):
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
        fn = getattr(module, "build_agent_task", None)
        if fn is None:
            fn = getattr(module, package.manifest.entrypoint_func, None)
        if not callable(fn):
            raise AttributeError(
                f"strategy {package.strategy_id!r}: missing build_agent_task(ctx)"
            )
        return fn

    def _kernel(self):
        if self.kernel_factory is not None:
            return self.kernel_factory(self.config)
        import importlib

        agent_mod = importlib.import_module("nerya.agent.kernel")
        skills_mod = importlib.import_module("nerya.skills.kernel")
        skills = self.skills or skills_mod.SkillKernel.boot(self.config)
        return agent_mod.AgentKernel(config=self.config, skills=skills)

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
        key.setdefault("market", market)
        key.setdefault("timeframe", timeframe)
        return key

    def _profile_for(self, package: StrategyPackage) -> dict[str, Any]:
        profile = package.manifest.agent_profile.asdict()
        policy = package.manifest.policy
        if not profile.get("title"):
            profile["title"] = f"{package.strategy_id} strategy agent"
        if not profile.get("role"):
            profile["role"] = "Execute strategy-generated trading tasks."
        if not profile.get("accounts"):
            profile["accounts"] = list(package.manifest.accounts)
        if not profile.get("markets"):
            profile["markets"] = list(package.manifest.markets)
        if not profile.get("allowed_tools"):
            profile["allowed_tools"] = [
                "portfolio_summary",
                "strategy_history",
                "risk_check",
                "trade_intent_submit",
            ]
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
    ) -> StrategyAgentTaskExecutionResult:
        error = {"code": code, "message": message}
        if trace:
            error["trace"] = trace
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
