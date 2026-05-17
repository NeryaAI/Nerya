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
from ..strategies.agent_task_mode import (
    AGENT_TASK_TARGET,
    agent_task_requested,
    agent_team_roles,
    legacy_agent_team_strategy,
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
        kernel = self._kernel()
        required_team_run = self._run_required_team(
            package=package,
            event=event,
            task=task,
            task_id=task_id,
            session_id=session_id,
            profile=profile,
            kernel=kernel,
        )
        if required_team_run is not None:
            task.prompt = self._required_team_decision_prompt(
                package=package,
                task=task,
                team_run=required_team_run,
            )
            task.metadata["required_team_run_id"] = required_team_run.get("team_run_id")
            task.metadata["required_team_run_ok"] = bool(required_team_run.get("ok"))
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
        module = self._load_strategy_module(package)
        fn = getattr(module, "build_agent_task", None)
        if callable(fn):
            raw = self._call_task_builder(package, fn, ctx)
            return StrategyAgentTask.from_value(raw)
        if legacy_agent_team_strategy(package.manifest):
            return self._default_agent_team_task(package, event, ctx)
        fn = getattr(module, package.manifest.entrypoint_func, None)
        if not callable(fn):
            raise AttributeError(
                f"strategy {package.strategy_id!r}: missing build_agent_task(ctx)"
            )
        raw = self._call_task_builder(package, fn, ctx)
        return StrategyAgentTask.from_value(raw)

    def _call_task_builder(self, package: StrategyPackage, fn: Any, ctx: Any) -> Any:
        def _call() -> Any:
            return fn(ctx)

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

    def _default_agent_team_task(
        self,
        package: StrategyPackage,
        event: TriggerEvent,
        ctx: Any,
    ) -> StrategyAgentTask:
        manifest = package.manifest
        payload = dict(event.payload or {})
        market = str(
            payload.get("market")
            or (manifest.markets[0] if manifest.markets else "")
        )
        timeframe = str(
            payload.get("timeframe")
            or payload.get("interval")
            or "1d"
        )
        account_id = str(
            payload.get("account_id")
            or (manifest.accounts[0] if manifest.accounts else "")
        )
        roles = agent_team_roles(manifest)
        snapshot = self._technical_snapshot(ctx, market=market, timeframe=timeframe)
        team_roles = [
            {
                "name": role,
                "instructions": self._role_task(role, market),
            }
            for role in roles
        ]
        prompt = "\n".join([
            f"Strategy Agent Team task for `{manifest.strategy_id}`.",
            "",
            "Do not decide from the trigger JSON alone. First inspect the",
            "available roles with `role_list` when needed, then call",
            "`team_run` with a real JSON roles array and the shared task below.",
            "For `team_run`, pass arguments.roles as the array itself; never",
            "wrap the roles array in a JSON string.",
            "",
            "Shared task:",
            f"- Market: {market or '<manifest market>'}",
            f"- Account: {account_id or '<manifest account>'}",
            f"- Timeframe: {timeframe}",
            "- Analyze technical trend/momentum/volume, fundamentals, macro",
            "  context, recent news/sentiment, and risk/position constraints.",
            "- Produce a team memo, then decide one action: buy, sell/reduce,",
            "  or hold.",
            "- If the team recommends buy or sell/reduce, call `risk_check`",
            "  before `trade_intent_submit`. Keep size inside policy limits",
            "  and use paper/shadow mode unless the runtime explicitly enables",
            "  live trading.",
            "- If confidence is below the strategy min confidence, hold.",
            "",
            "Suggested team_run roles JSON:",
            json.dumps(team_roles, ensure_ascii=False, indent=2, default=str),
            "",
            "Strategy policy:",
            json.dumps(manifest.policy.asdict(), ensure_ascii=False, indent=2, default=str),
            "",
            "Latest technical snapshot gathered by the strategy facade:",
            json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
            "",
            "Trigger payload:",
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            "",
            "Final response contract:",
            json.dumps(
                {
                    "decision": "buy|sell|reduce|hold",
                    "confidence": 0.0,
                    "team_run_id": "<id from team_run>",
                    "market": market,
                    "account_id": account_id,
                    "technical": "summary",
                    "fundamental": "summary",
                    "macro_news": "summary",
                    "risk": "summary",
                    "action_taken": "none|risk_check|trade_intent_submit",
                    "reasoning": ["short evidence-backed bullets"],
                },
                ensure_ascii=False,
                indent=2,
            ),
        ])
        return StrategyAgentTask.dispatch(
            prompt=prompt,
            session_key={"market": market, "timeframe": timeframe},
            metadata={
                "market": market,
                "timeframe": timeframe,
                "account_id": account_id,
                "roles": roles,
                "execution_mode": "agent_team_fallback",
                "trigger_event_id": event.event_id,
            },
            attached_skills=[
                "team",
                "trading",
                "market_research",
                "research",
                "market_data_routing",
            ],
            reason="legacy team strategy routed through AgentKernel/team_run",
        )

    @staticmethod
    def _role_task(role: str, market: str) -> str:
        role_key = role.lower()
        if "technical" in role_key or "quant" in role_key:
            return f"Analyze price action, indicators, trend, volume, and levels for {market}."
        if "fundamental" in role_key or "valuation" in role_key:
            return f"Analyze business fundamentals, valuation, earnings, and moat for {market}."
        if "macro" in role_key:
            return f"Analyze macro, rates, sector, and risk regime implications for {market}."
        if "news" in role_key or "sentiment" in role_key:
            return (
                f"Analyze recent news, filings, sentiment, and event risk for {market}. "
                "If a dedicated news skill is unavailable, use research or websearch; "
                "do not stop at a missing optional skill."
            )
        if "risk" in role_key or "critic" in role_key:
            return f"Challenge the trade, size, invalidation, and downside risks for {market}."
        return f"Contribute an evidence-backed investment view for {market}."

    @staticmethod
    def _technical_snapshot(ctx: Any, *, market: str, timeframe: str) -> dict[str, Any]:
        snapshot: dict[str, Any] = {"market": market, "timeframe": timeframe}
        if not market:
            return snapshot
        try:
            snapshot["features"] = ctx.market.features(
                market,
                timeframe=timeframe,
                lookback=160,
            )
        except Exception as exc:
            snapshot["features_error"] = f"{type(exc).__name__}: {exc}"
        try:
            candles = ctx.market.candles(
                market,
                timeframe=timeframe,
                limit=40,
            )
            snapshot["candles_count"] = len(candles)
            snapshot["recent_candles"] = list(candles[-12:])
        except Exception as exc:
            snapshot["candles_error"] = f"{type(exc).__name__}: {exc}"
        try:
            snapshot["ticker"] = ctx.market.ticker(market)
        except Exception as exc:
            snapshot["ticker_error"] = f"{type(exc).__name__}: {exc}"
        return snapshot

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
    ) -> dict[str, Any] | None:
        if not self._is_agent_team_task(task):
            return None
        roles = self._team_role_entries(package, task)
        if not roles:
            return None

        from ..skills.kernel import SkillKernel
        from ..tools.native.agents import team_run_handler

        skills = self.skills or getattr(kernel, "skills", None)
        if skills is None:
            skills = SkillKernel.boot(self.config)
        shared_payload = self._team_context_payload(package, event, task, task_id)
        call_id = f"toolu_required_team_{uuid.uuid4().hex[:12]}"
        call = ToolCall(
            name="team_run",
            id=call_id,
            caller="strategy.agent_task.required_team",
            arguments={
                "team_run_id": f"team-{task_id[-12:]}",
                "task": self._team_mission(package, task),
                "roles": roles,
                "shared_payload": shared_payload,
                "max_parallel": min(4, len(roles)),
                "strategy_id": package.strategy_id,
                "session_id": session_id,
                "trigger_event_id": event.event_id,
            },
            metadata={
                "strategy_id": package.strategy_id,
                "session_id": session_id,
                "trigger_event_id": event.event_id,
                "required_by": "strategy.agent_team",
            },
        )
        result = team_run_handler(
            call,
            config=self.config,
            skills=skills,
            tool_registry=getattr(kernel, "tool_registry", None),
        )
        data = self._tool_json_data(result)
        team_run_id = str(
            (data or {}).get("team_run_id")
            or call.arguments.get("team_run_id")
            or ""
        )
        ok = not bool(result.is_error)
        compact_data = self._compact_team_result(data)
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
    def _is_agent_team_task(task: StrategyAgentTask) -> bool:
        meta = dict(task.metadata or {})
        mode = str(meta.get("execution_mode") or "").strip().lower()
        if mode in {"agent_team", "agent_team_fallback"}:
            return True
        if meta.get("roles") and "team_run" in (task.prompt or ""):
            return True
        return False

    def _team_role_entries(
        self,
        package: StrategyPackage,
        task: StrategyAgentTask,
    ) -> list[dict[str, Any]]:
        meta = dict(task.metadata or {})
        raw_roles = meta.get("roles") or agent_team_roles(package.manifest)
        roles = [str(r).strip() for r in (raw_roles or []) if str(r).strip()]
        markets = self._task_markets(package, task)
        return [
            {
                "name": role,
                "instructions": self._role_task_for_markets(role, markets),
            }
            for role in roles
        ]

    @staticmethod
    def _role_task_for_markets(role: str, markets: list[str]) -> str:
        role_key = role.lower()
        universe = ", ".join(markets) or "<markets>"
        if "technical" in role_key or "quant" in role_key:
            return f"Rank technical trend, indicators, momentum, volume, and levels across: {universe}."
        if "fundamental" in role_key or "valuation" in role_key:
            return f"Compare fundamentals, valuation, earnings, and moat across: {universe}."
        if "macro" in role_key:
            return f"Analyze macro, rates, sector, and risk regime implications for the basket: {universe}."
        if "news" in role_key or "sentiment" in role_key:
            return f"Analyze recent live news, filings, sentiment, and event risk for the basket: {universe}."
        if "risk" in role_key or "critic" in role_key:
            return f"Challenge selected candidate, sizing, invalidation, concentration, and downside risks for: {universe}."
        return f"Contribute an evidence-backed basket view for: {universe}."

    def _team_context_payload(
        self,
        package: StrategyPackage,
        event: TriggerEvent,
        task: StrategyAgentTask,
        task_id: str,
    ) -> dict[str, Any]:
        markets = self._task_markets(package, task)
        timeframe = self._task_timeframe(package, event, task)
        account_id = self._task_account(package, task)
        ctx = build_strategy_context(
            config=self.config,
            package=package,
            skills=self.skills,
            run_id=f"{task_id}_team",
            session_id=None,
            connector_registry=self.connector_registry,
            trigger_event=event,
        )
        market_context: list[dict[str, Any]] = []
        for market in markets[:12]:
            item: dict[str, Any] = {"market": market, "timeframe": timeframe}
            try:
                item["features"] = ctx.market.features(
                    market,
                    timeframe=timeframe,
                    lookback=160,
                )
            except Exception as exc:
                item["features_error"] = f"{type(exc).__name__}: {exc}"
            try:
                candles = ctx.market.candles(market, timeframe=timeframe, limit=24)
                item["candles_count"] = len(candles)
                item["recent_candles"] = list(candles[-8:])
            except Exception as exc:
                item["candles_error"] = f"{type(exc).__name__}: {exc}"
            try:
                item["ticker"] = ctx.market.ticker(market)
            except Exception as exc:
                item["ticker_error"] = f"{type(exc).__name__}: {exc}"
            market_context.append(item)

        try:
            from ..strategies.performance import _build_news_context

            news_context = _build_news_context(package, config_like=self.config)
        except Exception as exc:
            news_context = {
                "items": [],
                "count": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "strategy_id": package.strategy_id,
            "markets": markets,
            "timeframe": timeframe,
            "account_id": account_id,
            "policy": package.manifest.policy.asdict(),
            "market_context": market_context,
            "news_context": news_context,
            "data_policy": "live market/news data only; no mock fallback accepted",
        }

    def _required_team_decision_prompt(
        self,
        *,
        package: StrategyPackage,
        task: StrategyAgentTask,
        team_run: dict[str, Any],
    ) -> str:
        shared = dict(team_run.get("shared_payload") or {})
        team_data = self._compact_team_result(team_run.get("data"))
        return "\n".join([
            f"Strategy Agent Team task for `{package.strategy_id}`.",
            "",
            "The runtime already executed the required `team_run` before this decision turn.",
            "Do not call `team_run` again unless the team run failed and you must retry a missing role.",
            "Use the live data context and Agent Team result below to decide one action.",
            "Before any buy/sell/reduce, call `risk_check`; call `trade_intent_submit` only if risk allows.",
            "Hold when confidence is below policy.min_confidence, evidence conflicts, or data is degraded.",
            "",
            "Live market/news context JSON:",
            json.dumps(self._compact_jsonable(shared), ensure_ascii=False, indent=2, default=str),
            "",
            "Required Agent Team result JSON:",
            json.dumps(team_data, ensure_ascii=False, indent=2, default=str),
            "",
            "Original strategy task metadata JSON:",
            json.dumps(dict(task.metadata or {}), ensure_ascii=False, indent=2, default=str),
            "",
            "Final response contract:",
            json.dumps(
                {
                    "decision": "buy|sell|reduce|hold",
                    "confidence": 0.0,
                    "team_run_id": team_run.get("team_run_id"),
                    "selected_market": "<best candidate or null>",
                    "ranked_candidates": [
                        {"market": "<symbol>", "rank": 1, "reason": "..."}
                    ],
                    "account_id": shared.get("account_id"),
                    "technical": "summary",
                    "fundamental": "summary",
                    "macro_news": "summary",
                    "risk": "summary",
                    "action_taken": "none|risk_check|trade_intent_submit",
                    "reasoning": ["evidence-backed bullet"],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
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
            or ("1d" if package.manifest.strategy_class == "agent_team" else "15m")
        )

    @staticmethod
    def _task_account(package: StrategyPackage, task: StrategyAgentTask) -> str:
        meta = dict(task.metadata or {})
        return str(
            meta.get("account_id")
            or (package.manifest.accounts[0] if package.manifest.accounts else "")
        )

    def _team_mission(self, package: StrategyPackage, task: StrategyAgentTask) -> str:
        markets = ", ".join(self._task_markets(package, task))
        timeframe = str(task.metadata.get("timeframe") or task.session_key.get("timeframe") or "1d")
        return (
            f"Analyze strategy {package.strategy_id} basket ({markets}) on {timeframe}; "
            "rank candidates using live K-line/ticker context, recent news, fundamentals, "
            "macro regime, and risk. Recommend buy/sell/reduce/hold with evidence."
        )

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
        use_agent_task = agent_task_requested(package.manifest)
        if not profile.get("title"):
            profile["title"] = f"{package.strategy_id} strategy agent"
        if not profile.get("role"):
            if use_agent_task:
                profile["role"] = (
                    "Run strategy-triggered market analysis through Agent Team, "
                    "then submit only risk-gated trade intents."
                )
            else:
                profile["role"] = "Execute strategy-generated trading tasks."
        if not profile.get("accounts"):
            profile["accounts"] = list(package.manifest.accounts)
        if not profile.get("markets"):
            profile["markets"] = list(package.manifest.markets)
        if not profile.get("allowed_tools"):
            profile["allowed_tools"] = [
                "role_list",
                "team_run",
                "market_data",
                "portfolio_summary",
                "strategy_history",
                "risk_check",
                "trade_intent_submit",
            ]
        elif use_agent_task:
            tools = list(profile.get("allowed_tools") or [])
            for tool in [
                "role_list",
                "team_run",
                "market_data",
                "portfolio_summary",
                "strategy_history",
                "risk_check",
                "trade_intent_submit",
            ]:
                if tool not in tools:
                    tools.append(tool)
            profile["allowed_tools"] = tools
        if use_agent_task:
            skills = list(profile.get("attached_skills") or [])
            for skill in [
                "team",
                "trading",
                "market_research",
                "research",
                "market_data_routing",
            ]:
                if skill not in skills:
                    skills.append(skill)
            profile["attached_skills"] = skills
            rules = list(profile.get("order_rules") or [])
            team_rule = (
                "For Agent Team strategies, call team_run and review the team "
                "memo before any risk_check or trade_intent_submit."
            )
            if team_rule not in rules:
                rules.append(team_rule)
            profile["order_rules"] = rules
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
