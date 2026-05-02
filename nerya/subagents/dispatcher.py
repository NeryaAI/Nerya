"""SubAgentDispatcher — resolves `subagent:<name>` targets and runs them.

native subagent runtime.

* Every subagent run produces a :class:`SubAgentResult` envelope with
  ``ok``, ``error_kind`` and ``error`` fields so the caller has a uniform
  failure story.
* ``dispatch_many`` runs multiple subagents concurrently (bounded by
  ``agent.subagents.max_parallel``) under a shared budget ceiling.
* A hard denylist prevents subagents from ever touching live-trading
  surfaces even if their spec allows it; those skills can only be called
  from the main agent.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ..core import jsonl
from ..core.config import Config
from ..llm.gateway import LLMGateway
from ..skills.kernel import SkillKernel
from ..strategy_history import store as history_store
from .registry import SubAgentSpec
from .runtime import SubAgentRuntime
from .strategy_registry import StrategySubAgentRegistry


# Skills that a subagent must never invoke directly. They are live-trading
# surfaces and must only be called from the main agent, where Risk/Approval
# gates and budgeting are already centralised.
#
# split ``trading`` into ``trading_read`` (allowed) and ``trading_write``
# (blocked). The dispatcher denies ``trading`` outright and separately
# denies ``trading_write``; subagents must use the read-only variant.
SUBAGENT_SKILL_DENYLIST: frozenset[str] = frozenset({
    "trading",           # legacy umbrella — fully blocked
    "trading_write",     # write-side surface — submit_intent / place_order
    "wallet",            # signer access
    "script_runtime",    # sandboxed script execution
})


@dataclass
class SubAgentResult:
    """Uniform envelope returned by every subagent run. extends this with per-run ``metrics`` and ``steps`` so the
    parent kernel can attribute contribution and reconstruct what
    happened inside a child runtime without having to reach into the
    LLM journal.
    """

    ok: bool
    subagent: str
    tier: str = "medium"
    output: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0
    usd: float = 0.0
    wall_ms: int = 0
    error: str | None = None
    error_kind: str | None = None  # "spec" | "llm" | "policy" | "unknown"
    metrics: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def _assert_allowed_skills(spec: SubAgentSpec) -> None:
    bad = [s for s in spec.allowed_skills if s in SUBAGENT_SKILL_DENYLIST]
    if bad:
        raise PermissionError(
            f"subagent {spec.name!r} is not allowed to use denylisted skills: "
            f"{bad}"
        )


@dataclass
class SubAgentDispatcher:
    config: Config
    skills: SkillKernel
    # Apr-30 2026 — operator directive: subagents must inherit the
    # parent's full native-tool surface (connector_list / connector_view,
    # memory, search, file primitives, …), not just the skill kernel.
    # The dispatcher accepts an optional ``tool_registry`` so the parent
    # kernel can pass its already-built native registry. When supplied,
    # the runtime falls through to native tools whenever the model
    # emits a ``skill_call`` whose ``skill`` name matches a registered
    # native tool. Default ``None`` keeps every existing test / call
    # site (which only ever passes ``config + skills``) working.
    tool_registry: Any = None

    def _registry_for(self, strategy_id: str | None) -> StrategySubAgentRegistry:
        """Build a fresh strategy-scoped registry for the active run.

        We don't cache across runs because a hot promotion can change
        the strategy package's prompt files; one registry per dispatch
        keeps the resolver honest while still letting a single dispatch
        reuse the same package handle for ``dispatch_many``.
        """

        return StrategySubAgentRegistry(
            paths=self.config.paths, strategy_id=strategy_id
        )

    def _resolve_spec(
        self, name: str, *, strategy_id: str | None = None
    ) -> SubAgentSpec:
        return self._registry_for(strategy_id).get(name)

    def _run_one(
        self, name: str, *, payload: dict[str, Any],
        trigger_event_id: str | None, strategy_id: str | None,
        session_id: str | None,
    ) -> SubAgentResult:
        import time as _t
        t0 = _t.monotonic()
        try:
            spec = self._resolve_spec(name, strategy_id=strategy_id)
            _assert_allowed_skills(spec)
            runtime = SubAgentRuntime(
                config=self.config, skills=self.skills,
                llm=LLMGateway(self.config),
                tool_registry=self.tool_registry,
            )
            raw = runtime.run(
                spec, trigger_event_id=trigger_event_id,
                payload=payload, strategy_id=strategy_id,
                session_id=session_id,
            )
            return SubAgentResult(
                ok=True,
                subagent=name,
                tier=str(raw.get("tier", spec.tier)),
                output=raw.get("output") or {},
                tokens=int(raw.get("tokens", 0) or 0),
                usd=float(raw.get("usd", 0.0) or 0.0),
                wall_ms=int((_t.monotonic() - t0) * 1000),
                metrics=(raw.get("metrics") or {}),
                steps=(raw.get("steps") or []),
                audit=(raw.get("audit") or {}),
            )
        except PermissionError as exc:
            return SubAgentResult(
                ok=False, subagent=name,
                error=str(exc), error_kind="policy",
                wall_ms=int((_t.monotonic() - t0) * 1000),
            )
        except Exception as exc:  # LLM errors, missing spec, IO, etc.
            return SubAgentResult(
                ok=False, subagent=name,
                error=f"{type(exc).__name__}: {exc}",
                error_kind="llm" if "LLM" in type(exc).__name__ else "unknown",
                wall_ms=int((_t.monotonic() - t0) * 1000),
            )

    def _journal(self, res: SubAgentResult, *,
                 trigger_event_id: str | None,
                 strategy_id: str | None, session_id: str | None) -> None:
        jsonl.append(self.config.paths.journal("agent"), {
            "kind": "subagent.run",
            "name": res.subagent,
            "trigger_event_id": trigger_event_id,
            "strategy_id": strategy_id, "session_id": session_id,
            "ok": res.ok,
            "tier": res.tier, "tokens": res.tokens, "usd": res.usd,
            "wall_ms": res.wall_ms,
            "error": res.error, "error_kind": res.error_kind,
            "metrics": res.metrics,
            "steps_count": len(res.steps),
        })
        if strategy_id and res.ok:
            history_store.record_subagent(
                self.config.paths, strategy_id=strategy_id,
                session_id=session_id, name=res.subagent,
                output=res.output or {},
            )

    # ------------------------------------------------------------------ api
    def dispatch(
        self, target: str, *,
        payload: dict[str, Any],
        trigger_event_id: str | None = None,
        strategy_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not target.startswith("subagent:"):
            return {"ok": False, "reason": "not_subagent_target"}
        name = target.split(":", 1)[1]
        res = self._run_one(
            name, payload=payload,
            trigger_event_id=trigger_event_id,
            strategy_id=strategy_id, session_id=session_id,
        )
        self._journal(
            res,
            trigger_event_id=trigger_event_id,
            strategy_id=strategy_id, session_id=session_id,
        )
        if not res.ok:
            # Backwards-compatible shape for existing callers.
            return {
                "subagent": name, "output": {}, "tier": res.tier,
                "tokens": 0, "usd": 0.0, "wall_ms": res.wall_ms,
                "metrics": res.metrics, "steps": res.steps,
                "audit": res.audit,
                "ok": False, "error": res.error, "error_kind": res.error_kind,
            }
        return {
            "subagent": name, "output": res.output, "tier": res.tier,
            "tokens": res.tokens, "usd": res.usd, "wall_ms": res.wall_ms,
            "metrics": res.metrics, "steps": res.steps, "audit": res.audit,
            "ok": True,
        }

    def dispatch_many(
        self,
        names: Iterable[str],
        *,
        payload: dict[str, Any],
        trigger_event_id: str | None = None,
        strategy_id: str | None = None,
        session_id: str | None = None,
        max_parallel: int | None = None,
        usd_budget: float | None = None,
    ) -> list[SubAgentResult]:
        """Run multiple subagents concurrently with a shared USD budget.

        * ``max_parallel`` defaults to ``config.agent.subagents.max_parallel``
          (or 4 if unset) and is capped at the number of subagents.
        * ``usd_budget`` defaults to ``config.agent.subagents.usd_budget``
          (or ``None`` for no cap).
        * Cancellation is cooperative: once the running total crosses the
          budget, no new subagent is started, but in-flight runs finish.
        """
        names_list = [n for n in names]
        if not names_list:
            return []
        if max_parallel is None:
            max_parallel = int(
                self.config.get("agent.subagents.max_parallel", 4) or 4
            )
        max_parallel = max(1, min(max_parallel, len(names_list)))
        if usd_budget is None:
            raw_budget = self.config.get("agent.subagents.usd_budget", None)
            usd_budget = float(raw_budget) if raw_budget is not None else None

        results: list[SubAgentResult] = []
        spent = 0.0
        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures = {
                pool.submit(
                    self._run_one, name,
                    payload=payload,
                    trigger_event_id=trigger_event_id,
                    strategy_id=strategy_id,
                    session_id=session_id,
                ): name
                for name in names_list
            }
            for fut in as_completed(futures):
                res = fut.result()
                results.append(res)
                self._journal(
                    res,
                    trigger_event_id=trigger_event_id,
                    strategy_id=strategy_id, session_id=session_id,
                )
                spent += res.usd
                if usd_budget is not None and spent >= usd_budget:
                    # Mark any pending futures as budget-skipped.
                    for pending_fut, pending_name in list(futures.items()):
                        if pending_fut.done() or pending_fut is fut:
                            continue
                        if pending_fut.cancel():
                            results.append(SubAgentResult(
                                ok=False, subagent=pending_name,
                                error_kind="budget",
                                error=f"subagent usd budget {usd_budget} exhausted",
                            ))
        # Stable order by original request order for deterministic tests.
        order = {n: i for i, n in enumerate(names_list)}
        results.sort(key=lambda r: (order.get(r.subagent, 0), r.subagent))
        return results
