"""SubAgentRuntime — a subagent runs as a real child runtime.

the subagent is no longer a single "build prompt → one LLM call →
return dict" black box. Each subagent:

* Owns its own iterative observe → think → act loop.
* Can dispatch a bounded set of allowed skills through the parent
  :class:`SkillRuntime` — with the parent's denylist still enforced by
  the dispatcher.
* Returns a structured envelope describing not only the final analysis
  but also *contribution metrics*: signals consumed, skill calls made,
  rejected actions, residual uncertainty, and evidence references.

The runtime is deliberately conservative: it runs at most
``max_iterations`` think steps, caps the number of tool calls per run,
and hard-stops on budget/policy errors. The parent kernel remains the
only place a live-trading surface can be reached.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.config import Config
from ..core.time import now_iso
from ..llm.gateway import LLMGateway
from ..security.prompt_injection import wrap_untrusted
from ..skills.kernel import SkillKernel
from .context_policy import build_context
from .registry import SubAgentSpec


# Skills that the subagent is never allowed to dispatch directly, even when
# they are listed in ``spec.allowed_skills``. Mirrors the parent dispatcher
# denylist so we reject attempts at the child-runtime layer too.
#
# trading is split into ``trading_read`` (allowed for analyst lanes) and
# ``trading_write`` (blocked everywhere except the main agent). We deny
# both the legacy umbrella and the write surface here.
CHILD_SKILL_DENYLIST: frozenset[str] = frozenset({
    "trading", "trading_write", "wallet", "script_runtime",
})

# Apr-27 2026 — operator directive: read-only "self-control" skills must
# be available to every subagent regardless of whether the registry
# allowlist remembered to include them. Mirrors
# ``nerya.agent.skill_selector._CORE_SELF_CONTROL_SKILLS`` so a subagent
# can always:
#   * introspect the workspace (``workspace`` — list strategies / scripts
#     / triggers / accounts so it knows what already exists before
#     authoring new artifacts),
#   * fetch the full SKILL.md for any tool (``skill_index`` — the
#     documented escape hatch when the model needs the precise schema),
#   * pull live web evidence to ground a claim before reporting back
#     (``websearch`` — DuckDuckGo by default, no API key required).
# These are all read-only, so they're safe to grant universally. The
# operator can still blacklist them via ``skills.disabled`` or per-spec
# ``allowed_skills`` overrides if they need a hard-locked subagent.
CHILD_CORE_SELF_CONTROL_SKILLS: tuple[str, ...] = (
    "workspace", "skill_index", "websearch",
)


# Apr-30 2026 — operator directive: subagents must inherit the *full*
# native-tool surface the parent has so e.g. ``market_analyst`` and
# ``risk_critic`` can call ``connector_list`` / ``connector_view`` /
# ``memory_*`` / ``recipe_view`` mid-investigation. Without this the
# child was forced to assume a venue was missing because the only
# bridge into the registry lived on the parent kernel's tool list.
#
# The denylist keeps the destructive surface off-limits regardless of
# parent permissions: live trading writes (``trading_open_*``,
# ``trading_cancel_*``, ``trading_set_*``), evolution promote/rollback
# and the LLM delegation tools that already proxy through the
# subagent path (avoiding accidental fan-out).
CHILD_NATIVE_TOOL_DENYLIST_PREFIXES: tuple[str, ...] = (
    "trading_open", "trading_cancel", "trading_set", "trading_cleanup",
    "wallet_",
    "evolve_promote", "evolve_rollback",
    # Children should not spawn more children directly — that path
    # only exists on the parent so the dispatcher can budget total
    # subagent fan-out.
    "subagent_run",
)
# Risk levels the child may invoke directly. Anything DANGEROUS is
# always denied, no matter how the parent classified it.
CHILD_NATIVE_TOOL_DENY_RISK: tuple[str, ...] = ("dangerous",)


@dataclass
class _StepRecord:
    kind: str                # "think" | "act" | "observe" | "close"
    iteration: int
    status: str = "ok"
    detail: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0
    usd: float = 0.0
    error: str | None = None
    wall_ms: int = 0
    ts: str = field(default_factory=now_iso)

    def asdict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "iteration": self.iteration,
            "status": self.status,
            "detail": self.detail,
            "tokens": self.tokens,
            "usd": self.usd,
            "error": self.error,
            "wall_ms": self.wall_ms,
            "ts": self.ts,
        }


@dataclass
class SubAgentRuntime:
    config: Config
    skills: SkillKernel
    llm: LLMGateway
    # Apr-30 2026 — passed in by the parent kernel via ``SubAgentDispatcher``
    # so the child can invoke native tools (connector_list / connector_view /
    # memory_* / recipe_view / search / read / glob / grep …) directly
    # instead of having to find a skill that wraps them. Optional so the
    # legacy callsites (``SubAgentRuntime(config=..., skills=..., llm=...)``)
    # keep working — when ``None`` the runtime simply has no native-tool
    # fallthrough and the child is restricted to the skill kernel.
    tool_registry: Any = None

    # ---------------------------------------------------------------- config
    def _max_iterations(self) -> int:
        # Apr-27 2026 (third bump) — coding-agent's ``forkSubagent``
        # (src/tools/AgentTool/forkSubagent.ts:65) ships ``maxTurns: 200``
        # and the runtime never auto-terminates a subagent below that ceiling.
        # When we asked Nerya to run a "deep research on $TICKER" team plan
        # the subagent kept stopping mid-investigation because 20 was still
        # too tight: it spent its first few turns picking sources, then needed
        # 1–2 turns per source to fetch + cross-check, then 1–2 turns to
        # summarise. 60 gives the LLM enough headroom to author a small
        # script in workspace, run it, scan results, retry on failures, and
        # produce a structured report — without becoming truly unbounded.
        return max(1, int(self.config.get(
            "agent.subagents.max_iterations", 60,
        ) or 60))

    def _max_skill_calls(self) -> int:
        # Aligned with the iteration bump: a research subagent now has room
        # to sequence ``operator.write_file`` → ``operator.run_python`` →
        # ``websearch.search`` → ``operator.read_file`` → ``write_file``
        # several times before summarising. Hard cap stays in place so a
        # genuinely runaway loop still trips an error.
        return max(0, int(self.config.get(
            "agent.subagents.max_skill_calls", 120,
        ) or 120))

    # ---------------------------------------------------------------- core
    def run(
        self,
        spec: SubAgentSpec,
        *,
        trigger_event_id: str | None,
        payload: dict[str, Any],
        strategy_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        t_start = time.monotonic()
        steps: list[_StepRecord] = []
        # Apr-29 2026 — operator directive: ``spec.allowed_skills`` is now
        # a *preload / preference* list, not a hard allowlist. The
        # subagent (and team members that funnel through this runtime)
        # gets the *full* skill catalogue minus :data:`CHILD_SKILL_DENYLIST`,
        # so authoring a persona no longer requires guessing which
        # skills it might need. The list still drives the prompt copy
        # so the model can be nudged toward the operator's preferred
        # tools without being locked out of the long tail.
        preloaded = [s for s in spec.allowed_skills if s not in CHILD_SKILL_DENYLIST]
        for sid in CHILD_CORE_SELF_CONTROL_SKILLS:
            if sid in CHILD_SKILL_DENYLIST:
                continue
            if sid in preloaded:
                continue
            preloaded.append(sid)
        # Build the universe of skills the runtime is willing to dispatch.
        # Denylist still wins.
        try:
            registry_ids = [
                str(getattr(e, "id", "") or "")
                for e in self.skills.registry.list()
            ]
        except Exception:
            registry_ids = []
        callable_skills = sorted({
            sid for sid in registry_ids
            if sid and sid not in CHILD_SKILL_DENYLIST
        })
        # Apr-30 2026 — also surface the native-tool surface the parent
        # passed in. The child can invoke any of these via the same
        # ``skill_calls`` JSON envelope (the dispatcher decides whether
        # the name resolves to a skill or a native tool). We compute
        # the visible set once per run so the prompt copy is stable
        # across iterations.
        callable_native_tools = self._allowed_native_tool_names()
        skill_calls: list[dict[str, Any]] = []
        rejected_actions: list[dict[str, Any]] = []
        signals_used: list[str] = []
        evidence: list[dict[str, Any]] = []
        uncertainty: float = 0.0

        # Apr-27 2026 — emit subagent lifecycle events on the same
        # process-wide streaming bus the parent kernel uses, so the
        # dashboard / gateway / TUI can display the subagent's *own*
        # think → act → observe loop live (chat transcript order).
        # Without this the subagent appeared as one opaque
        # "subagent X ran" badge regardless of how many internal
        # iterations it ran. We never let a streaming-bus failure
        # break a subagent run: every publish call is wrapped.
        try:
            from ..agent.streaming import get_default_bus
            _bus = get_default_bus()
        except Exception:
            _bus = None  # type: ignore[assignment]
        team_event_fields = {
            "team_run_id": payload.get("team_run_id"),
            "team_template": payload.get("team_template"),
            "team_task_id": payload.get("task_id"),
            "team_task_owner": payload.get("task_owner"),
            "team_task_subject": payload.get("task_subject"),
        }

        def _publish(kind: str, **fields: Any) -> None:
            if _bus is None:
                return
            try:
                _bus.publish(
                    kind,
                    subagent=spec.name,
                    tier=spec.tier,
                    strategy_id=strategy_id,
                    session_id=session_id,
                    trigger_event_id=trigger_event_id,
                    **{k: v for k, v in team_event_fields.items() if v is not None},
                    **fields,
                )
            except Exception:
                pass

        _publish("subagent.start", payload_keys=sorted(payload.keys()))

        base_context = build_context(
            self.config, self.skills, spec,
            payload=payload, strategy_id=strategy_id,
        )

        max_iter = self._max_iterations()
        max_calls = self._max_skill_calls()
        last_parsed: dict[str, Any] = {}
        last_raw: str = ""
        total_tokens = 0
        total_usd = 0.0

        accumulated_obs: list[dict[str, Any]] = []
        for i in range(max_iter):
            prompt = self._render_prompt(
                spec, payload, base_context, accumulated_obs,
                allowed=preloaded,
                native_tools=callable_native_tools,
            )
            t0 = time.monotonic()
            try:
                result = self.llm.call(
                    task="subagent_analysis", caller=f"subagent:{spec.name}",
                    tier=spec.tier, prompt=prompt,
                )
            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {exc}"
                steps.append(_StepRecord(
                    kind="think", iteration=i, status="error",
                    error=err_msg,
                    wall_ms=int((time.monotonic() - t0) * 1000),
                ))
                _publish(
                    "subagent.step",
                    step_kind="think", iteration=i, status="error",
                    error=err_msg,
                    wall_ms=int((time.monotonic() - t0) * 1000),
                )
                break

            total_tokens += int(result.tokens or 0)
            total_usd += float(result.usd or 0.0)
            parsed = result.parsed if isinstance(result.parsed, dict) else {}
            last_parsed = parsed
            last_raw = result.raw
            think_wall = int((time.monotonic() - t0) * 1000)
            steps.append(_StepRecord(
                kind="think", iteration=i, status="ok",
                tokens=int(result.tokens or 0),
                usd=float(result.usd or 0.0),
                wall_ms=think_wall,
                detail={"keys": sorted(parsed.keys())[:8]},
            ))
            # Surface the model's chain-of-thought live. Mirrors what
            # ``nerya.agent.kernel`` publishes for the main agent's
            # think/replan steps so ``LiveActivity`` can render the
            # subagent's reasoning inline.
            _reasoning_text = ""
            try:
                _reasoning_text = str(getattr(result, "reasoning_text", "") or "")
            except Exception:
                _reasoning_text = ""
            _publish(
                "subagent.step",
                step_kind="think", iteration=i, status="ok",
                tokens=int(result.tokens or 0),
                usd=float(result.usd or 0.0),
                wall_ms=think_wall,
                reasoning=_reasoning_text[:4000] if _reasoning_text else "",
                reasoning_tokens=int(getattr(result, "reasoning_tokens", 0) or 0),
                reasoning_effort=str(
                    getattr(result, "reasoning_effort", "") or ""
                ),
                provider=str(getattr(result, "provider", "") or ""),
                model=str(getattr(result, "model", "") or ""),
                parsed_keys=sorted(parsed.keys())[:12],
            )

            # Pick up contribution metadata the subagent produced.
            for sig in _coerce_list(parsed.get("signals") or parsed.get("signals_used")):
                if sig not in signals_used:
                    signals_used.append(str(sig))
            for ev in _coerce_list(parsed.get("evidence")):
                if isinstance(ev, dict):
                    evidence.append(ev)
                else:
                    evidence.append({"note": str(ev)})
            try:
                u = parsed.get("uncertainty")
                if u is not None:
                    uncertainty = max(0.0, min(1.0, float(u)))
            except Exception:
                pass

            # Dispatch any requested skill calls. Only allowed skills that are
            # not denylisted will run. Every attempt is recorded either as a
            # skill_call entry or as a rejected_actions entry.
            actions = _coerce_list(parsed.get("skill_calls") or parsed.get("tool_calls"))
            batch_obs: list[dict[str, Any]] = []
            for entry in actions:
                if len(skill_calls) >= max_calls:
                    rejected_actions.append({
                        "entry": entry, "reason": "skill_call_budget_exhausted",
                    })
                    break
                record = self._dispatch_one(
                    entry, spec_name=spec.name, allowed=callable_skills,
                    allowed_native_tools=callable_native_tools,
                    strategy_id=strategy_id, session_id=session_id,
                    trigger_event_id=trigger_event_id,
                )
                if record is None:
                    continue
                if record.get("ok"):
                    skill_calls.append(record)
                    batch_obs.append({
                        "iteration": i,
                        "skill": record.get("skill"),
                        "action": record.get("action"),
                        "ok": True,
                        "summary": _summarise(record.get("result")),
                    })
                    _publish(
                        "subagent.step",
                        step_kind="act", iteration=i, status="ok",
                        skill=record.get("skill"),
                        action=record.get("action"),
                    )
                else:
                    rejected_actions.append(record)
                    batch_obs.append({
                        "iteration": i,
                        "skill": record.get("skill"),
                        "action": record.get("action"),
                        "ok": False,
                        "error": record.get("error"),
                    })
                    _publish(
                        "subagent.step",
                        step_kind="act", iteration=i, status="error",
                        skill=record.get("skill"),
                        action=record.get("action"),
                        error=str(record.get("error") or ""),
                    )
            if batch_obs:
                steps.append(_StepRecord(
                    kind="act", iteration=i, status="ok",
                    detail={"observations_count": len(batch_obs)},
                ))
                accumulated_obs.extend(batch_obs)

            # Respect an explicit "done" signal; otherwise continue only if
            # the subagent explicitly asked for another pass.
            if parsed.get("done") is True or parsed.get("final") is True:
                break
            if not (parsed.get("continue") or parsed.get("replan")):
                break

        steps.append(_StepRecord(
            kind="close", iteration=len(steps),
            wall_ms=int((time.monotonic() - t_start) * 1000),
            detail={
                "iterations": sum(1 for s in steps if s.kind == "think"),
                "skill_calls": len(skill_calls),
                "rejected_actions": len(rejected_actions),
            },
        ))
        _publish(
            "subagent.step",
            step_kind="close",
            iteration=len(steps),
            wall_ms=int((time.monotonic() - t_start) * 1000),
            iterations=sum(1 for s in steps if s.kind == "think"),
            skill_calls_n=len(skill_calls),
            rejected_actions_n=len(rejected_actions),
            tokens=total_tokens,
            usd=total_usd,
        )
        _publish(
            "subagent.end",
            iterations=sum(1 for s in steps if s.kind == "think"),
            skill_calls=len(skill_calls),
            rejected=len(rejected_actions),
            tokens=total_tokens,
            usd=total_usd,
            wall_ms=int((time.monotonic() - t_start) * 1000),
        )

        return {
            "subagent": spec.name,
            "tier": spec.tier,
            "output": last_parsed or {"raw": last_raw},
            "tokens": total_tokens,
            "usd": total_usd,
            "metrics": {
                "signals_used": signals_used,
                "skill_calls": skill_calls,
                "rejected_actions": rejected_actions,
                "uncertainty": uncertainty,
                "evidence": evidence,
                "iterations": sum(1 for s in steps if s.kind == "think"),
            },
            "steps": [s.asdict() for s in steps],
        }

    # ---------------------------------------------------------------- prompt
    def _render_prompt(
        self,
        spec: SubAgentSpec,
        payload: dict[str, Any],
        context: str,
        observations: list[dict[str, Any]],
        *,
        allowed: list[str],
        native_tools: list[str] | None = None,
    ) -> str:
        obs_block = ""
        if observations:
            obs_block = (
                "\n=== prior observations ===\n"
                + json.dumps(observations[-6:], ensure_ascii=False, default=str)
                + "\n"
            )
        # Apr-30 2026 — surface the native-tool surface inherited from
        # the parent kernel so the child can self-discover venues
        # (connector_list/connector_view), read memory, browse recipes,
        # run shell, glob/grep, etc., without us having to ship a
        # skill wrapper for every native tool.
        nt_block = ""
        if native_tools:
            preview = ", ".join(native_tools[:48])
            tail = (
                f" (+{len(native_tools) - 48} more)"
                if len(native_tools) > 48 else ""
            )
            nt_block = (
                "\nNative tools (parent kernel inheritance — call them "
                "via the same ``skill_calls`` envelope using the tool "
                f"name as the ``skill`` field): {preview}{tail}.\n"
                "Particularly useful: ``connector_list`` and "
                "``connector_view`` to discover already-integrated "
                "exchanges / data sources before claiming something is "
                "missing."
            )
        allow_note = (
            "\nYou may request skill calls via JSON "
            "``{\"skill_calls\": [{\"skill\": <id>, \"action\": <name>, "
            "\"payload\": {...}}]}``. "
            f"Preloaded skills (the operator preloaded these for this role; "
            f"prefer them when relevant): {allowed or 'none'}. "
            "You can call any other workspace skill too — the runtime only "
            "blocks trading_write / wallet / script_runtime — so reach for "
            "the long tail when the task needs it. "
            f"{nt_block}"
            "\nIf you are done, include ``\"done\": true``; to re-plan after "
            "these calls, include ``\"replan\": true``."
        )
        return (
            f"You are the {spec.name} subagent.\n"
            f"{spec.prompt or ''}\n\n"
            f"=== task payload ===\n"
            f"{wrap_untrusted('payload', json.dumps(payload, ensure_ascii=False, default=str))}\n\n"
            f"=== context ===\n{context}\n"
            f"{obs_block}{allow_note}\n"
        )

    # ---------------------------------------------------------------- dispatch
    def _allowed_native_tool_names(self) -> list[str]:
        """Return the subset of parent native tools children may invoke.

        Apr-30 2026 — the child inherits the parent's native-tool
        surface (connector_list / connector_view / memory_* /
        recipe_view / read / glob / grep / search / shell …) so it
        can self-discover venues mid-run. The destructive surface
        (live trading writes, evolve_promote, subagent_run) and any
        DANGEROUS-tier tool stays parent-only — the dispatcher itself
        plus :data:`CHILD_NATIVE_TOOL_DENYLIST_PREFIXES` enforce that.
        """

        registry = self.tool_registry
        if registry is None:
            return []
        try:
            tools = registry.list_tools()
        except Exception:
            return []
        out: list[str] = []
        for d in tools:
            name = str(getattr(d, "name", "") or "")
            if not name:
                continue
            if any(name.startswith(p) for p in CHILD_NATIVE_TOOL_DENYLIST_PREFIXES):
                continue
            risk = str(getattr(getattr(d, "risk", None), "value", "") or "")
            if risk and risk.lower() in CHILD_NATIVE_TOOL_DENY_RISK:
                continue
            out.append(name)
        return sorted(out)

    def _dispatch_one(
        self,
        entry: Any,
        *,
        spec_name: str,
        allowed: list[str],
        allowed_native_tools: list[str] | None = None,
        trigger_event_id: str | None,
        strategy_id: str | None,
        session_id: str | None,
    ) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return {
                "ok": False, "skill": None, "action": None,
                "error": "skill_call entry is not a dict",
                "entry": entry,
            }
        skill = str(entry.get("skill") or entry.get("skill_id") or "").strip()
        action = str(entry.get("action") or entry.get("name") or "").strip()
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        if not skill:
            return {
                "ok": False, "skill": skill or None, "action": action or None,
                "error": "skill missing",
                "entry": entry,
            }
        if skill in CHILD_SKILL_DENYLIST:
            return {
                "ok": False, "skill": skill, "action": action,
                "error": "skill is in child denylist", "entry": entry,
            }
        # Native-tool fallthrough — Apr-30 2026 operator directive:
        # subagents inherit the parent's native-tool surface so e.g.
        # ``market_analyst`` can call ``connector_list`` mid-run. The
        # child speaks the same ``skill_calls`` envelope; we resolve
        # to the tool registry first and fall back to the skill kernel
        # only when the name is unknown to the native registry.
        native_names = allowed_native_tools or []
        if skill in native_names:
            return self._dispatch_native(
                skill, payload=payload or {}, entry=entry,
                spec_name=spec_name,
                strategy_id=strategy_id, session_id=session_id,
                trigger_event_id=trigger_event_id,
            )
        if not action:
            return {
                "ok": False, "skill": skill, "action": None,
                "error": (
                    "action missing (and skill name does not match a "
                    "native tool the child is allowed to invoke)"
                ),
                "entry": entry,
            }
        # ``allowed`` here is the full set of skills the runtime is
        # willing to dispatch (registry minus denylist). It is *not*
        # the operator's per-spec preload list — that's a hint surfaced
        # in the prompt, not a hard wall. So we only fail closed when
        # the requested id is genuinely unknown to the registry.
        if allowed and skill not in allowed:
            return {
                "ok": False, "skill": skill, "action": action,
                "error": (
                    f"skill {skill!r} is not registered with the "
                    f"workspace skill runtime (or is denylisted for "
                    f"subagents)"
                ),
                "entry": entry,
            }
        try:
            result = self.skills.runtime.call(
                skill, action, payload=payload or {},
                caller=f"subagent:{spec_name}",
                strategy_id=strategy_id,
                session_id=session_id,
                trigger_event_id=trigger_event_id,
            )
        except Exception as exc:
            return {
                "ok": False, "skill": skill, "action": action,
                "error": f"{type(exc).__name__}: {exc}",
                "entry": entry,
            }
        return {
            "ok": True, "skill": skill, "action": action,
            "result": result,
        }

    def _dispatch_native(
        self,
        tool_name: str,
        *,
        payload: dict[str, Any],
        entry: dict[str, Any],
        spec_name: str,
        strategy_id: str | None,
        session_id: str | None,
        trigger_event_id: str | None,
    ) -> dict[str, Any]:
        """Invoke a parent native tool from inside a subagent.

        We call the tool's handler directly (synchronously) with a
        :class:`ToolCall` envelope. Any handler error is captured and
        surfaced back through the same observation-record shape the
        skill path uses, so the child's prompt-rendering code does not
        need to special-case native results.
        """

        registry = self.tool_registry
        if registry is None:
            return {
                "ok": False, "skill": tool_name, "action": "(native)",
                "error": "no native tool registry inherited from parent",
                "entry": entry,
            }
        try:
            descriptor = registry.get(tool_name)
        except Exception as exc:
            return {
                "ok": False, "skill": tool_name, "action": "(native)",
                "error": f"native tool not found: {exc}",
                "entry": entry,
            }
        from ..tools.types import ToolCall  # local import to avoid cycles

        call = ToolCall(
            name=tool_name,
            arguments=dict(payload or {}),
            caller=f"subagent:{spec_name}",
            metadata={
                "subagent": spec_name,
                "strategy_id": strategy_id,
                "session_id": session_id,
                "trigger_event_id": trigger_event_id,
            },
        )
        try:
            raw = descriptor.handler(call)
            if hasattr(raw, "__await__"):
                # Async handlers exist (some shell tools). We don't
                # have an event loop here, so we run them inline.
                import asyncio

                loop = asyncio.new_event_loop()
                try:
                    raw = loop.run_until_complete(raw)  # type: ignore[arg-type]
                finally:
                    loop.close()
        except Exception as exc:
            return {
                "ok": False, "skill": tool_name, "action": "(native)",
                "error": f"{type(exc).__name__}: {exc}",
                "entry": entry,
            }
        # Render the ToolResult into a dict the rest of the runtime
        # (``_summarise``, the journalled metrics block) can consume.
        result_dict = _tool_result_to_dict(raw)
        if result_dict.get("is_error"):
            return {
                "ok": False, "skill": tool_name, "action": "(native)",
                "error": result_dict.get("error_message")
                or "native tool returned is_error=true",
                "result": result_dict,
                "entry": entry,
            }
        return {
            "ok": True, "skill": tool_name, "action": "(native)",
            "result": result_dict,
        }


def _tool_result_to_dict(result: Any) -> dict[str, Any]:
    """Render a :class:`nerya.tools.types.ToolResult` into a plain dict.

    The subagent runtime stores observations as JSON-friendly dicts so
    the prompt renderer can ``json.dumps`` them directly. Native-tool
    handlers return :class:`ToolResult` objects whose ``content`` is a
    list of :class:`ToolResultPart` (text / json). We pick the first
    json part if present (most native tools return JSON) and fall back
    to concatenated text. The ``is_error`` / ``error_message`` shape is
    preserved so :meth:`SubAgentRuntime._dispatch_native` can route the
    record through the rejected-actions path.
    """

    if result is None:
        return {"is_error": True, "error_message": "handler returned None"}
    # Try the canonical asdict() path first.
    asdict = getattr(result, "asdict", None)
    raw_dict: dict[str, Any]
    if callable(asdict):
        try:
            raw_dict = asdict()
        except Exception:
            raw_dict = {}
    else:
        raw_dict = {}
    if not raw_dict:
        # Best-effort fallback: introspect known fields.
        raw_dict = {
            "is_error": bool(getattr(result, "is_error", False)),
            "name": getattr(result, "name", ""),
            "content": [],
        }
    is_error = bool(raw_dict.get("is_error"))
    error_message = ""
    err = raw_dict.get("error")
    if isinstance(err, dict):
        error_message = str(err.get("message") or err.get("kind") or "")
    parts = raw_dict.get("content") or []
    payload: Any = None
    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "json" and "data" in part and payload is None:
            payload = part.get("data")
        elif part.get("type") == "text":
            text_parts.append(str(part.get("text") or ""))
    out: dict[str, Any] = {
        "is_error": is_error,
        "name": raw_dict.get("name") or "",
    }
    if payload is not None:
        out["data"] = payload
    if text_parts:
        out["text"] = "\n".join(text_parts)
    if error_message and not out.get("text"):
        out["error_message"] = error_message
    return out


def _coerce_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, (str, int, float, bool)):
        return [v]
    if isinstance(v, dict):
        return [v]
    return []


def _summarise(result: Any, *, limit: int = 4000) -> str:
    """Render a tool-call result for the subagent's next-iteration prompt.

    Apr-27 2026: bumped from 200 → 4000 chars. The 200-char limit was a
    silent killer for the code-driven research lane: when the
    ``market_analyst`` subagent ran ``operator.terminal`` to execute a
    Python fetcher, the ``stdout`` field (which holds the actual JSON
    payload — top-5 symbols, ticker quotes, headlines, …) routinely runs
    1-3 KB. Truncating to 200 chars meant the subagent's LLM literally
    couldn't see what its own script printed, so it would either reply
    "no data" or rerun the script in another loop — exactly what we saw
    on Hyperliquid. 4000 is enough for ~5 ticker rows / a small RSS
    digest while still bounding context growth.

    For dict results we pull payload-bearing keys (``stdout``, ``data``,
    ``items``, ``rows``, ``snapshot``) out first so they're never the
    field that gets clipped, then append the rest of the envelope. Lists
    and scalars fall through to the original JSON dump.
    """
    if isinstance(result, dict):
        priority_keys = (
            "stdout", "data", "items", "rows", "snapshot",
            "ticker", "fills", "headlines", "top5", "top_5",
        )
        head: list[str] = []
        rest: dict[str, Any] = {}
        for k, v in result.items():
            if k in priority_keys:
                try:
                    serialised = json.dumps(v, ensure_ascii=False, default=str)
                except Exception:
                    serialised = repr(v)
                head.append(f'"{k}": {serialised}')
            else:
                rest[k] = v
        try:
            rest_text = json.dumps(rest, ensure_ascii=False, default=str)
        except Exception:
            rest_text = repr(rest)
        text = "{" + ", ".join(head) + (", _envelope: " + rest_text if rest else "") + "}"
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            text = repr(result)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text
