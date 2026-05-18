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
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.config import Config
from ..core.redaction import redact_display_dict, redact_text
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

# Always expose a small read-only "self-control" skill set to every
# subagent so it can inspect the workspace, load skill docs, and fetch
# live web evidence even if the preferred skill list omitted them.
# A subagent can always:
#   * introspect the workspace (``workspace`` — list strategies / scripts
#     / triggers / accounts so it knows what already exists before
#     authoring new artifacts),
#   * fetch the full SKILL.md for any tool (``skill_index`` — the
#     documented escape hatch when the model needs the precise schema),
#   * pull live web evidence to ground a claim before reporting back
#     (``web_search`` / ``web_search_fetch`` — current native web tools).
# These are all read-only, so they're safe to grant universally. The
# operator can still blacklist them via ``skills.disabled`` or per-spec
# ``allowed_skills`` overrides if they need a hard-locked subagent.
CHILD_CORE_SELF_CONTROL_SKILLS: tuple[str, ...] = (
    "workspace", "skill_index", "web_search", "web_search_fetch",
)


# Subagents inherit the parent's full native-tool surface so roles such
# as ``market_analyst`` and ``risk_critic`` can call
# ``connector_list`` / ``connector_view`` / ``memory_*`` /
# ``recipe_view`` mid-investigation. Without this, the child would have
# to assume a venue or data source was missing because the registry only
# existed on the parent.
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

STOCK_RESEARCH_SUBAGENTS: frozenset[str] = frozenset({
    "technical_analyst",
    "fundamentals_analyst",
    "sentiment_analyst",
    "bull_researcher",
    "bear_researcher",
    "risk_critic",
    "research_manager",
    "research_editor",
})

LEGACY_TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "websearch": ("web_search", "web_search_fetch"),
    "news_social": ("web_search_fetch",),
    "portfolio": ("portfolio_summary",),
    "risk": ("risk_check",),
    "trading_read": ("portfolio_summary",),
}


class SubAgentLLMError(RuntimeError):
    """Raised when a child runtime cannot produce any model output."""


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
    # Passed in by the parent kernel so the child can invoke native tools
    # (connector_list / connector_view / memory_* / recipe_view / search /
    # read / glob / grep …) directly instead of relying on skill wrappers.
    # Optional so legacy callsites keep working — when ``None`` the runtime
    # simply has no native-tool fallthrough and the child is restricted to
    # the skill kernel.
    tool_registry: Any = None

    # ---------------------------------------------------------------- config
    def _max_iterations(self, spec: SubAgentSpec | None = None) -> int:
        # Keep the default ceiling high enough for research-oriented
        # subagents to inspect sources, run small scripts, retry failures,
        # and still produce a structured report without stopping mid-run.
        explicit = self.config.get("agent.subagents.max_iterations", None)
        if explicit is not None:
            return max(1, int(explicit or 1))
        if spec is not None and spec.name in STOCK_RESEARCH_SUBAGENTS:
            return max(1, int(self.config.get(
                "agent.subagents.stock_research_max_iterations", 8,
            ) or 8))
        return max(1, int(60))

    def _max_skill_calls(self) -> int:
        # Aligned with the iteration bump: a research subagent now has room
        # to sequence ``operator.write_file`` → ``operator.run_python`` →
        # ``websearch.search`` → ``operator.read_file`` → ``write_file``
        # several times before summarising. Hard cap stays in place so a
        # genuinely runaway loop still trips an error.
        return max(0, int(self.config.get(
            "agent.subagents.max_skill_calls", 120,
        ) or 120))

    def _max_wall_seconds(self, spec: SubAgentSpec | None = None) -> float:
        explicit = self.config.get("agent.subagents.max_wall_seconds", None)
        if explicit is not None:
            try:
                return max(5.0, float(explicit or 5))
            except Exception:
                return 120.0
        if spec is not None and spec.name in STOCK_RESEARCH_SUBAGENTS:
            try:
                return max(5.0, float(self.config.get(
                    "agent.subagents.stock_research_max_wall_seconds", 240,
                ) or 240))
            except Exception:
                return 240.0
        return max(5.0, float(600))

    # ---------------------------------------------------------------- core
    def run(
        self,
        spec: SubAgentSpec,
        *,
        trigger_event_id: str | None,
        payload: dict[str, Any],
        strategy_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> dict[str, Any]:
        t_start = time.monotonic()
        steps: list[_StepRecord] = []
        # Treat ``spec.allowed_skills`` as a preload / preference list, not
        # a hard allowlist. The subagent gets the full callable catalogue
        # minus :data:`CHILD_SKILL_DENYLIST`; the preferred list only nudges
        # prompt-time tool selection.
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
        # Also surface the native-tool set inherited from the parent. The
        # child uses the same ``skill_calls`` JSON envelope; the dispatcher
        # decides whether each name resolves to a skill or a native tool.
        # Compute the visible set once per run so the prompt copy is stable
        # across iterations.
        callable_native_tools = self._allowed_native_tool_names()
        preloaded = _normalise_preloaded_tools(
            preloaded,
            callable_skills=callable_skills,
            native_tools=callable_native_tools,
        )
        skill_calls: list[dict[str, Any]] = []
        rejected_actions: list[dict[str, Any]] = []
        signals_used: list[str] = []
        evidence: list[dict[str, Any]] = []
        uncertainty: float = 0.0

        # Emit subagent lifecycle events on the same process-wide
        # streaming bus the parent kernel uses so the dashboard /
        # gateway / TUI can display the subagent's own think → act →
        # observe loop live. We never let a streaming-bus failure
        # break a subagent run: every publish call is wrapped.
        try:
            from ..agent.streaming import get_default_bus
            _bus = get_default_bus()
        except Exception:
            _bus = None  # type: ignore[assignment]
        team_event_fields = {
            "turn_id": turn_id,
            "team_run_id": payload.get("team_run_id"),
            "team_template": payload.get("team_template"),
            "team_call_id": payload.get("team_call_id") or parent_call_id,
            "team_task_id": payload.get("task_id"),
            "team_task_owner": payload.get("task_owner"),
            "team_task_subject": payload.get("task_subject"),
        }
        try:
            safe_payload = redact_display_dict(payload)
        except Exception:
            safe_payload = payload
        audit_payload = safe_payload if isinstance(safe_payload, dict) else payload

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

        base_context = build_context(
            self.config, self.skills, spec,
            payload=payload, strategy_id=strategy_id,
        )

        max_iter = self._max_iterations(spec)
        max_calls = self._max_skill_calls()
        max_wall_seconds = self._max_wall_seconds(spec)
        consecutive_unproductive_batches = 0
        last_parsed: dict[str, Any] = {}
        last_raw: str = ""
        fatal_llm_error: str | None = None
        close_reason: str | None = None
        total_tokens = 0
        total_usd = 0.0
        audit_prompts: list[dict[str, Any]] = []
        audit_start = {
            "subagent": spec.name,
            "tier": spec.tier,
            "prompt_path": str(spec.prompt_path) if spec.prompt_path else "",
            "role_prompt": redact_text(spec.prompt or ""),
            "payload": safe_payload,
            "payload_keys": sorted(payload.keys()),
            "allowed_skills": list(spec.allowed_skills or []),
            "callable_skills": callable_skills,
            "native_tools": callable_native_tools,
            "context_chars": len(base_context or ""),
            "redacted": True,
        }

        _publish(
            "subagent.start",
            payload_keys=audit_start["payload_keys"],
            payload=audit_start["payload"],
            role_prompt=audit_start["role_prompt"],
            prompt_path=audit_start["prompt_path"],
            allowed_skills=audit_start["allowed_skills"],
            callable_skills=audit_start["callable_skills"],
            native_tools=audit_start["native_tools"],
            context_chars=audit_start["context_chars"],
        )

        accumulated_obs: list[dict[str, Any]] = []
        for entry in _stock_research_data_prefetch_calls(
            spec.name,
            payload,
            native_tools=callable_native_tools,
        ):
            if len(skill_calls) >= max_calls:
                rejected_actions.append({
                    "entry": entry,
                    "reason": "skill_call_budget_exhausted",
                })
                break
            record = self._dispatch_one(
                entry,
                spec_name=spec.name,
                allowed=callable_skills,
                allowed_native_tools=callable_native_tools,
                strategy_id=strategy_id,
                session_id=session_id,
                trigger_event_id=trigger_event_id,
            )
            if record is None:
                continue
            if record.get("ok"):
                skill_calls.append(record)
                accumulated_obs.append({
                    "iteration": "prefetch",
                    "skill": record.get("skill"),
                    "action": record.get("action"),
                    "ok": True,
                    "summary": _summarise(record.get("result")),
                })
                _publish(
                    "subagent.step",
                    step_kind="act",
                    iteration=-1,
                    status="ok",
                    skill=record.get("skill"),
                    action=record.get("action"),
                )
            else:
                rejected_actions.append(record)
                accumulated_obs.append({
                    "iteration": "prefetch",
                    "skill": record.get("skill"),
                    "action": record.get("action"),
                    "ok": False,
                    "error": record.get("error"),
                })
                _publish(
                    "subagent.step",
                    step_kind="act",
                    iteration=-1,
                    status="error",
                    skill=record.get("skill"),
                    action=record.get("action"),
                    error=str(record.get("error") or ""),
                )
        for i in range(max_iter):
            if time.monotonic() - t_start >= max_wall_seconds:
                close_reason = "subagent_wall_time_exceeded"
                steps.append(_StepRecord(
                    kind="close",
                    iteration=i,
                    status="error",
                    error="subagent_wall_time_exceeded",
                    wall_ms=int((time.monotonic() - t_start) * 1000),
                    detail={"max_wall_seconds": max_wall_seconds},
                ))
                break
            prompt = self._render_prompt(
                spec, payload, base_context, accumulated_obs,
                allowed=preloaded,
                native_tools=callable_native_tools,
            )
            audit_prompt = self._render_prompt(
                spec, audit_payload, base_context, accumulated_obs,
                allowed=preloaded,
                native_tools=callable_native_tools,
            )
            safe_prompt = redact_text(audit_prompt)
            audit_prompts.append({
                "iteration": i,
                "prompt": safe_prompt,
                "prompt_chars": len(audit_prompt),
                "redacted": True,
            })
            _publish(
                "subagent.step",
                step_kind="prompt",
                iteration=i,
                status="sent",
                prompt=safe_prompt,
                prompt_chars=len(audit_prompt),
                payload=safe_payload,
            )
            t0 = time.monotonic()
            try:
                result = self.llm.call(
                    task="subagent_analysis", caller=f"subagent:{spec.name}",
                    tier=spec.tier, prompt=prompt,
                )
            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {exc}"
                fatal_llm_error = err_msg
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
            legacy_tool_calls = _extract_legacy_tool_calls(result.raw)
            if legacy_tool_calls and _should_use_legacy_tool_calls(parsed):
                parsed = {
                    "skill_calls": legacy_tool_calls,
                    "replan": True,
                }
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
            batch_success = False
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
                    batch_success = True
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
                    detail={
                        "observations_count": len(batch_obs),
                        "successful": batch_success,
                    },
                ))
                accumulated_obs.extend(batch_obs)

            # Respect an explicit "done" signal; otherwise continue only if
            # the subagent explicitly asked for another pass. When the model
            # requested tool/skill calls we always give it one more turn with
            # those observations, even if it forgot to set replan=true.
            if parsed.get("done") is True or parsed.get("final") is True:
                break
            if batch_obs:
                if batch_success:
                    consecutive_unproductive_batches = 0
                else:
                    consecutive_unproductive_batches += 1
                    if consecutive_unproductive_batches >= 2:
                        close_reason = "repeated_failed_tool_batches"
                        break
                continue
            if not (parsed.get("continue") or parsed.get("replan")):
                break
        if close_reason is None:
            if sum(1 for s in steps if s.kind == "think") >= max_iter:
                close_reason = "max_iterations"

        steps.append(_StepRecord(
            kind="close", iteration=len(steps),
            wall_ms=int((time.monotonic() - t_start) * 1000),
            detail={
                "iterations": sum(1 for s in steps if s.kind == "think"),
                "skill_calls": len(skill_calls),
                "rejected_actions": len(rejected_actions),
                "close_reason": close_reason,
            },
        ))
        final_output = _final_subagent_output(last_parsed, last_raw)
        if final_output.get("degraded") and skill_calls:
            final_output = _tool_observation_fallback_output(
                spec_name=spec.name,
                payload=payload,
                observations=accumulated_obs,
                skill_calls=skill_calls,
                rejected_actions=rejected_actions,
                close_reason=close_reason,
            )
        final_output = _attach_data_coverage(
            final_output,
            skill_calls=skill_calls,
            rejected_actions=rejected_actions,
        )
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
            error=fatal_llm_error,
            close_reason=close_reason,
        )
        _publish(
            "subagent.end",
            iterations=sum(1 for s in steps if s.kind == "think"),
            skill_calls=len(skill_calls),
            rejected=len(rejected_actions),
            tokens=total_tokens,
            usd=total_usd,
            error=fatal_llm_error,
            wall_ms=int((time.monotonic() - t_start) * 1000),
            output=redact_display_dict(final_output),
            metrics=redact_display_dict({
                "signals_used": signals_used,
                "skill_calls": skill_calls,
                "rejected_actions": rejected_actions,
                "uncertainty": uncertainty,
                "evidence": evidence,
            }),
        )

        if fatal_llm_error and not last_parsed and not last_raw:
            raise SubAgentLLMError(
                f"subagent {spec.name} LLM call failed before producing output: "
                f"{fatal_llm_error}"
            )

        return {
            "subagent": spec.name,
            "tier": spec.tier,
            "output": final_output,
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
            "audit": {
                **audit_start,
                "prompt_records": audit_prompts,
                "redacted": True,
            },
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
        # Surface the native-tool set inherited from the parent kernel so
        # the child can self-discover venues, read memory, browse recipes,
        # run shell, and use file primitives without a skill wrapper for
        # every native tool.
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
            hints = _native_tool_usage_hints(native_tools)
            if hints:
                nt_block += "\n" + hints
        output_language = str(
            payload.get("output_language")
            or payload.get("target_language")
            or payload.get("response_language")
            or ""
        ).strip()
        language_block = ""
        if output_language:
            language_block = (
                "=== output language ===\n"
                f"Target user-visible language: {output_language}\n"
                "Write natural-language JSON values, evidence summaries, "
                "rationales, and role conclusions in this language. Preserve "
                "JSON keys, required enum values, proper nouns, tickers, "
                "source names, code identifiers, URLs, and numeric metrics.\n\n"
            )
        allow_note = (
            "\nYou may request skill calls via JSON "
            "``{\"skill_calls\": [{\"skill\": <id>, \"action\": <name>, "
            "\"payload\": {...}}]}``. "
            f"Preferred callable tools for this role: {allowed or 'none'}. "
            "Use exact tool names and fields; do not invent legacy names "
            "such as ``websearch`` / ``news_social`` or guessed actions "
            "such as ``market_data.get_quote``. If a workspace skill only "
            "describes a playbook, use it as context and call the native "
            "tools below rather than guessing action names. "
            f"{nt_block}"
            "\nIf you are done, include ``\"done\": true``; to re-plan after "
            "these calls, include ``\"replan\": true``."
        )
        if observations:
            allow_note += (
                "\nYou already have tool observations. Prefer producing the "
                "final role analysis now with ``\"done\": true``. Do not "
                "request the same data again; if a field is missing, state "
                "the evidence gap instead of looping on more tools."
            )
        return (
            f"You are the {spec.name} subagent.\n"
            f"{spec.prompt or ''}\n\n"
            f"=== task payload ===\n"
            f"{wrap_untrusted('payload', json.dumps(payload, ensure_ascii=False, default=str))}\n\n"
            f"{language_block}"
            f"=== context ===\n{context}\n"
            f"{obs_block}{allow_note}\n"
        )

    # ---------------------------------------------------------------- dispatch
    def _allowed_native_tool_names(self) -> list[str]:
        """Return the subset of parent native tools children may invoke.

        The child inherits the parent's native-tool surface
        (connector_list / connector_view / memory_* / recipe_view /
        read / glob / grep / search / shell …) so it can self-discover
        venues mid-run. The destructive surface (live trading writes,
        evolve_promote, subagent_run) and any DANGEROUS-tier tool stays
        parent-only — the dispatcher itself
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
        # Native-tool fallthrough: subagents inherit the parent's
        # native-tool surface, so ``market_analyst`` can call
        # ``connector_list`` mid-run. Resolve against the tool registry
        # first and fall back to the skill kernel only when the name is
        # unknown to the native registry.
        native_names = allowed_native_tools or []
        if skill in native_names:
            native_payload = dict(payload or {})
            if action and "action" not in native_payload:
                native_payload["action"] = action
            return self._dispatch_native(
                skill, payload=native_payload, entry=entry,
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

        payload = _normalise_native_payload(tool_name, payload)
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


def _normalise_preloaded_tools(
    values: list[str],
    *,
    callable_skills: list[str],
    native_tools: list[str],
) -> list[str]:
    """Keep role hints on the real callable surface.

    Several older default role specs still say ``websearch`` /
    ``news_social`` / ``portfolio`` even though the current native tool
    surface exposes ``web_search`` / ``web_search_fetch`` /
    ``portfolio_summary``. Showing stale names in the child prompt trains the
    model to call tools that cannot exist, so normalize aliases and drop
    non-callable leftovers before rendering the prompt.
    """

    callable_set = set(callable_skills) | set(native_tools)
    out: list[str] = []
    for raw in values:
        name = str(raw or "").strip()
        if not name:
            continue
        candidates = LEGACY_TOOL_ALIASES.get(name, (name,))
        for candidate in candidates:
            if candidate not in callable_set:
                continue
            if candidate not in out:
                out.append(candidate)
    return out


def _native_tool_usage_hints(native_tools: list[str]) -> str:
    available = set(native_tools or [])
    lines: list[str] = [
        "Common native-tool examples for research roles:",
    ]
    if "market_data" in available:
        lines.append(
            "- market_data: "
            '{"skill":"market_data","payload":{"action":"get_candles",'
            '"venue":"yahoo","market":"NVDA","interval":"1d","count":90}}; '
            "actions are get_ticker, get_mark_price, get_candles, "
            "calculate_features, summarize_market, compress_context."
        )
    if "data_api" in available:
        lines.append(
            "- data_api: for non-OHLC provider-specific data. For "
            "on-chain/meme/DEX wallet sources first inspect "
            '{"skill":"data_api","payload":{"op":"list","provider":"wallet"}} '
            "and "
            '{"skill":"data_api","payload":{"op":"list","provider":"onchainos"}}; '
            "aliases include xagt_agent_plugin, xagent, okx_os, okx_onchain. "
            "For wallet-backed meme strategies, first call "
            'data_api wallet.capability_catalog with {"topic":"meme"} or '
            "wallet.meme_strategy_guide so you use the selected route for the "
            "installed/logged-in wallet; when none is ready, follow the "
            "GOAT/self_custody fallback and wallet install recommendations."
        )
    if "web_search_fetch" in available:
        lines.append(
            "- web_search_fetch: "
            '{"skill":"web_search_fetch","payload":{"query":"NVIDIA NVDA '
            'latest earnings data center revenue guidance","max_results":5,'
            '"fetch_top_n":3}}.'
        )
    elif "web_search" in available:
        lines.append(
            "- web_search: "
            '{"skill":"web_search","payload":{"query":"NVIDIA NVDA latest '
            'earnings data center revenue guidance","max_results":5}}.'
        )
    if "mcp__yahoo__get_stock_info" in available:
        lines.append(
            "- Yahoo MCP direct tools use ticker, not symbol: "
            '{"skill":"mcp__yahoo__get_stock_info","payload":{"ticker":"NVDA"}}.'
        )
    if "mcp__yahoo__get_financial_statement" in available:
        lines.append(
            "- Yahoo statements: "
            '{"skill":"mcp__yahoo__get_financial_statement","payload":'
            '{"ticker":"NVDA","financial_type":"income_stmt"}}; also use '
            "balance_sheet or cashflow."
        )
    if any(t.startswith("mcp__edgar__") for t in available):
        lines.append(
            "- Edgar MCP direct tools use identifier, not ticker/cik: "
            '{"skill":"mcp__edgar__get_company_info","payload":{"identifier":"NVDA"}}.'
        )
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _prefetch_symbol(payload: dict[str, Any]) -> str:
    for key in ("ticker", "symbol", "identifier"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _stock_research_data_prefetch_calls(
    spec_name: str,
    payload: dict[str, Any],
    *,
    native_tools: list[str],
) -> list[dict[str, Any]]:
    if spec_name not in {"fundamentals_analyst", "risk_critic"}:
        return []
    symbol = _prefetch_symbol(payload)
    if not symbol:
        return []
    available = set(native_tools or [])
    venue = str(payload.get("venue") or "yahoo")
    calls: list[dict[str, Any]] = []

    def add(skill: str, payload: dict[str, Any]) -> None:
        if skill not in available:
            return
        calls.append({"skill": skill, "payload": payload})

    add("market_data", {"action": "get_ticker", "venue": venue, "market": symbol})

    if spec_name in {"risk_critic", "technical_analyst"}:
        add(
            "market_data",
            {
                "action": "get_candles",
                "venue": venue,
                "market": symbol,
                "interval": "1d",
                "count": 90,
            },
        )

    if spec_name == "fundamentals_analyst":
        add("mcp__yahoo__get_stock_info", {"ticker": symbol})
        for financial_type in ("income_stmt", "balance_sheet", "cashflow"):
            add(
                "mcp__yahoo__get_financial_statement",
                {"ticker": symbol, "financial_type": financial_type},
            )

    return calls


def _normalise_native_payload(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Repair common LLM-emitted aliases before native schema validation."""

    out = dict(payload or {})
    name = str(tool_name or "")

    if name == "market_data":
        action = str(out.get("action") or "").strip()
        action_aliases = {
            "get_quote": "get_ticker",
            "get_price": "get_ticker",
            "quote": "get_ticker",
            "price": "get_ticker",
            "get_history": "get_candles",
            "get_historical_data": "get_candles",
            "historical_data": "get_candles",
            "get_klines": "get_candles",
            "klines": "get_candles",
            "history": "get_candles",
            "get_features": "calculate_features",
            "technical_indicators": "calculate_features",
        }
        if action in action_aliases:
            out["action"] = action_aliases[action]
        elif not action:
            out["action"] = (
                "get_candles"
                if any(k in out for k in ("interval", "count", "limit", "period", "range"))
                else "get_ticker"
            )
        if "market" not in out:
            if out.get("symbol"):
                out["market"] = out.get("symbol")
            elif out.get("ticker"):
                out["market"] = out.get("ticker")
        period = str(out.get("period") or out.get("range") or "").lower()
        if period and "count" not in out and "limit" not in out:
            if "6mo" in period or "6m" in period:
                out["count"] = 180
            elif "3mo" in period or "3m" in period:
                out["count"] = 90
            elif "1y" in period or "12mo" in period:
                out["count"] = 252
        return out

    if name.startswith("mcp__"):
        out.pop("action", None)

    if name.startswith("mcp__yahoo__"):
        if "ticker" not in out and out.get("symbol"):
            out["ticker"] = out.get("symbol")
        if name.endswith("__get_financial_statement"):
            raw = str(
                out.get("financial_type")
                or out.get("statement_type")
                or out.get("statement")
                or ""
            ).strip().lower()
            statement_aliases = {
                "income": "income_stmt",
                "income_statement": "income_stmt",
                "income statement": "income_stmt",
                "balance": "balance_sheet",
                "balance_sheet": "balance_sheet",
                "balance sheet": "balance_sheet",
                "cashflow": "cashflow",
                "cash_flow": "cashflow",
                "cash flow": "cashflow",
                "cashflow_statement": "cashflow",
                "cashflow_stmt": "cashflow",
            }
            if raw:
                out["financial_type"] = statement_aliases.get(raw, raw)
        if name.endswith("__get_holder_info") and not out.get("holder_type"):
            out["holder_type"] = "institutional_holders"
        if name.endswith("__get_recommendations") and not out.get("recommendation_type"):
            out["recommendation_type"] = "recommendations"

    if name.startswith("mcp__edgar__"):
        if "identifier" not in out:
            if out.get("ticker"):
                out["identifier"] = out.get("ticker")
            elif out.get("symbol"):
                out["identifier"] = out.get("symbol")
            elif out.get("cik"):
                out["identifier"] = out.get("cik")

    return out


def _final_subagent_output(parsed: dict[str, Any], raw: str) -> dict[str, Any]:
    if isinstance(parsed, dict) and parsed:
        analytical_keys = {
            "summary", "recommendation", "confidence", "thesis",
            "invalidation", "risk_flags", "evidence", "done", "final",
        }
        raw_text = " ".join(
            str(parsed.get(key) or "")
            for key in ("raw", "text", "message", "content")
        )
        if not (set(parsed) & analytical_keys) and (
            parsed.get("skill_calls") or parsed.get("tool_calls")
            or "<tool_call" in raw_text
            or '"skill_calls"' in raw_text
            or '"tool_calls"' in raw_text
        ):
            return {
                "raw": str(raw or ""),
                "degraded": True,
                "error_kind": "unfinished_tool_request",
                "summary": (
                    "subagent requested tool calls but did not produce a "
                    "final analysis"
                ),
                "requested_tools": parsed.get("skill_calls") or parsed.get("tool_calls"),
            }
        return parsed
    text = str(raw or "")
    if text.strip():
        return {"raw": text}
    return {
        "raw": "",
        "degraded": True,
        "error_kind": "empty_model_output",
        "summary": "subagent finished without visible final output",
    }


def _tool_observation_fallback_output(
    *,
    spec_name: str,
    payload: dict[str, Any],
    observations: list[dict[str, Any]],
    skill_calls: list[dict[str, Any]],
    rejected_actions: list[dict[str, Any]],
    close_reason: str | None,
) -> dict[str, Any]:
    subject = (
        payload.get("ticker")
        or payload.get("market")
        or payload.get("company")
        or payload.get("task_subject")
        or payload.get("__team_task")
        or ""
    )
    successful_tools = [
        {
            "skill": rec.get("skill"),
            "action": rec.get("action"),
            "summary": _summarise(rec.get("result")),
        }
        for rec in skill_calls[-12:]
        if rec.get("ok")
    ]
    failed_tools = [
        {
            "skill": rec.get("skill"),
            "action": rec.get("action"),
            "error": str(rec.get("error") or "")[:500],
        }
        for rec in rejected_actions[-6:]
    ]
    return {
        "summary": (
            f"{spec_name} collected tool observations for {subject or 'the task'} "
            "but did not emit a final narrative before its budget ended. "
            "Use the observations below as evidence-backed partial findings "
            "and state any remaining gap explicitly."
        ),
        "done": True,
        "partial": True,
        "quality": "tool_observation_fallback",
        "role": spec_name,
        "subject": subject,
        "close_reason": close_reason or "unfinished_tool_request",
        "observations": observations[-12:],
        "tools_used": successful_tools,
        "tool_errors": failed_tools,
    }


def _attach_data_coverage(
    output: dict[str, Any],
    *,
    skill_calls: list[dict[str, Any]],
    rejected_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(output, dict):
        return output
    tools_used = [
        {
            "skill": rec.get("skill"),
            "action": rec.get("action"),
            "ok": True,
        }
        for rec in skill_calls[-16:]
        if rec.get("ok")
    ]
    tool_errors = [
        {
            "skill": rec.get("skill"),
            "action": rec.get("action"),
            "error": str(rec.get("error") or "")[:500],
        }
        for rec in rejected_actions[-8:]
    ]
    skills = {
        str(rec.get("skill") or "")
        for rec in skill_calls
        if rec.get("ok")
    }
    coverage = {
        "tools_used": tools_used,
        "tool_errors": tool_errors,
        "has_market_data": "market_data" in skills,
        "has_financial_statement": (
            "mcp__yahoo__get_financial_statement" in skills
        ),
        "has_stock_info": "mcp__yahoo__get_stock_info" in skills,
    }
    merged = dict(output)
    merged["data_coverage"] = coverage
    return merged


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


_LEGACY_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=([A-Za-z0-9_.:-]+)>(.*?)</function>\s*</tool_call>",
    re.DOTALL,
)
_LEGACY_SKILL_CALLS_RE = re.compile(
    r"<skill_calls>\s*(.*?)\s*</skill_calls>",
    re.DOTALL,
)
_LEGACY_TOOL_PARAM_RE = re.compile(
    r"<parameter=([A-Za-z0-9_.:-]+)>(.*?)</parameter>",
    re.DOTALL,
)


def _extract_legacy_tool_calls(raw: str) -> list[dict[str, Any]]:
    """Translate XML-ish model tool-call text into the child JSON envelope.

    Some providers occasionally emit a Claude/OpenAI-looking textual block
    instead of the subagent runtime's documented ``skill_calls`` JSON. Treating
    that raw text as final output makes Agent Team roles look successful even
    though they only asked to use a tool. This compatibility shim keeps the
    execution loop moving through the normal dispatcher and observation path.
    """

    text = str(raw or "")
    if "<tool_call>" not in text and "<skill_calls>" not in text:
        return []
    out: list[dict[str, Any]] = []
    for block in _LEGACY_SKILL_CALLS_RE.finditer(text):
        parsed = _parse_legacy_skill_calls_block(block.group(1))
        for entry in parsed:
            if isinstance(entry, dict):
                out.append(entry)
    for match in _LEGACY_TOOL_CALL_RE.finditer(text):
        skill = match.group(1).strip()
        body = match.group(2)
        if not skill:
            continue
        payload: dict[str, Any] = {}
        action = ""
        for param in _LEGACY_TOOL_PARAM_RE.finditer(body):
            key = param.group(1).strip()
            if not key:
                continue
            value = _parse_legacy_tool_param(param.group(2))
            if key == "action":
                action = str(value or "").strip()
            else:
                payload[key] = value
        call: dict[str, Any] = {"skill": skill, "payload": payload}
        if action:
            call["action"] = action
        out.append(call)
    return out


def _parse_legacy_skill_calls_block(raw: str) -> list[Any]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        return []
    if isinstance(parsed, dict):
        return _coerce_list(parsed.get("skill_calls") or parsed.get("tool_calls"))
    if isinstance(parsed, list):
        return parsed
    return []


def _should_use_legacy_tool_calls(parsed: dict[str, Any]) -> bool:
    """Return true when parsed content is only a raw-text wrapper.

    Some provider adapters preserve non-JSON assistant text as
    ``{"raw": "..."}`` instead of leaving ``parsed`` empty. If that raw
    text contains XML-ish tool calls, treating it as final output makes a
    role look complete even though it only asked to use tools.
    """

    if not parsed:
        return True
    if parsed.get("skill_calls") or parsed.get("tool_calls"):
        return False
    if parsed.get("done") is True or parsed.get("final") is True:
        return False
    raw_only_keys = {"raw", "text", "message", "content"}
    return set(parsed).issubset(raw_only_keys)


def _parse_legacy_tool_param(raw: str) -> Any:
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        return json.loads(value)
    except Exception:
        return value


def _summarise(result: Any, *, limit: int = 4000) -> str:
    """Render a tool-call result for the subagent's next-iteration prompt.

    The preview budget is large enough to preserve payload-bearing
    ``stdout`` / ``data`` for script-driven research without dumping
    unbounded output into the next prompt. 4000 chars is usually enough
    for a small table, quote set, or RSS digest while still bounding
    context growth.

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
