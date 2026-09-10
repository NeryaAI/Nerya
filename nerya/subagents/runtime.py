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
import math
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from ..core.config import Config
from ..harness.cancellation import is_cancelled as _token_is_set
from ..core.redaction import redact_display_dict, redact_text
from ..core.time import now_iso
from ..llm.attempt_budget import DEFAULT_EXTRA_ATTEMPT_LIMIT
from ..llm.gateway import LLMGateway
from ..llm.route_candidates import configured_models, configured_routes
from ..security.prompt_injection import wrap_untrusted
from ..skills.kernel import SkillKernel
from ..agent.runtime import CompletionGateLike
from .registry import SubAgentExecutionPolicy, SubAgentSpec


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
# These are read-only, so they're safe to grant universally. The
# operator can still blacklist them via ``skills.disabled`` or per-spec
# ``allowed_skills`` overrides if they need a hard-locked subagent.
CHILD_CORE_SELF_CONTROL_SKILLS: tuple[str, ...] = (
    "workspace", "skill_index",
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
    "subagent_run", "team_run",
)
# Risk levels the child may invoke directly. Anything DANGEROUS is
# always denied, no matter how the parent classified it.
CHILD_NATIVE_TOOL_DENY_RISK: tuple[str, ...] = ("dangerous",)

SUBAGENT_FINALIZATION_RESERVE_SECONDS = 45.0
SubAgentContextScope = Literal["subagent", "explicit_payload_only"]
DEFAULT_CONTEXT_SCOPE: SubAgentContextScope = "subagent"
EXPLICIT_PAYLOAD_ONLY_CONTEXT_SCOPE: SubAgentContextScope = "explicit_payload_only"


def _token_reason(token: Any) -> str:
    return str(getattr(token, "reason", "") or "cancelled")


_TASK_CONTROL_PAYLOAD_KEYS: frozenset[str] = frozenset({
    "__team_instructions",
    "__team_task",
    "analysis_language",
    "discussion_language",
    "internal_language",
    "original_user_prompt",
    "original_user_request",
    "open_work_items",
    "output_language",
    "research_requirements",
    "response_language",
    "target_language",
    "task_id",
    "task_owner",
    "task_subject",
    "team_call_id",
    "team_run_id",
    "team_template",
    "working_language",
})

def _split_task_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate trusted parent orchestration from untrusted data payload."""

    task_envelope: dict[str, Any] = {}
    data_payload: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        normalized = str(key)
        if normalized in _TASK_CONTROL_PAYLOAD_KEYS or normalized.startswith("__team_"):
            task_envelope[normalized] = value
        else:
            data_payload[normalized] = value
    return task_envelope, data_payload


def _native_tool_records(
    blocks: list[Any],
    *,
    caller: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project canonical loop blocks onto the existing child metrics shape."""

    payload_by_call_id: dict[str, Any] = {}
    approval_by_call_id: dict[str, dict[str, Any]] = {}
    normalized_blocks: list[dict[str, Any]] = []
    successful: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for envelope in blocks or []:
        block = getattr(envelope, "block", None)
        if not isinstance(block, dict) and isinstance(envelope, dict):
            block = envelope.get("block", envelope)
        if not isinstance(block, dict):
            continue
        normalized_blocks.append(block)
        call_id = str(block.get("call_id") or "")
        if block.get("kind") == "tool_use" and call_id:
            payload_by_call_id[call_id] = block.get("payload") or {}
        elif block.get("kind") == "approval_request" and call_id:
            approval_by_call_id[call_id] = dict(block)

    for block in normalized_blocks:
        if block.get("kind") != "tool_result":
            continue
        call_id = str(block.get("call_id") or "")
        tool_name = str(block.get("action") or block.get("skill_id") or "")
        result = _native_result_record(
            block.get("result"),
            tool_name=tool_name,
            ok=bool(block.get("ok")),
        )
        record = {
            "ok": bool(block.get("ok")),
            "skill": tool_name,
            "action": "(native)",
            "tool_use_id": call_id,
            "caller": caller,
            "payload": redact_display_dict(payload_by_call_id.get(call_id, {})),
            "result": result,
        }
        if record["ok"]:
            successful.append(record)
            continue
        record.update({
            "error": block.get("error") or "native tool failed",
            "error_kind": block.get("error_kind"),
            "recovery_hint": block.get("recovery") or {},
        })
        approval_request = approval_by_call_id.get(call_id)
        if approval_request is not None:
            record["approval_request"] = redact_display_dict(
                approval_request
            )
        rejected.append(record)
    return successful, rejected


def _native_result_record(
    value: Any,
    *,
    tool_name: str,
    ok: bool,
) -> dict[str, Any]:
    """Project a native result into the public child audit record."""

    base: dict[str, Any] = {"is_error": not ok, "name": tool_name}
    if isinstance(value, dict):
        if "data" in value or "is_error" in value:
            return {**base, **value}
        return {**base, "data": value}
    if isinstance(value, list):
        return {**base, "data": value}

    text = str(value or "").strip()
    candidates = [text]
    marker = "[compacted_kept]"
    if marker in text:
        candidates.insert(0, text.split(marker, 1)[1].strip())
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, (dict, list)):
            return {**base, "data": parsed}
    if text:
        base["text"] = text
    return base


def _native_final_output(final_text: str, *, stop_reason: str) -> dict[str, Any]:
    """Preserve structured role output while accepting useful plain prose."""

    from ..llm.structured_output import parse

    text = str(final_text or "").strip()
    try:
        parsed = parse(text, strict=False)
    except Exception:
        parsed = {"raw": text}
    if isinstance(parsed, dict) and set(parsed) != {"raw"}:
        output = dict(parsed)
    elif text:
        output = {"summary": text, "raw": text}
    else:
        output = {
            "summary": "subagent stopped without visible final output",
            "raw": "",
            "degraded": True,
        }
    output.setdefault("done", stop_reason == "end_turn")
    if stop_reason != "end_turn":
        output.update(done=False, degraded=True, error_kind=stop_reason or "incomplete")
    elif not text:
        output.update(done=False, error_kind="empty_model_output")
    return output


def _render_subagent_task_assignment(
    *,
    spec_name: str,
    task_envelope: dict[str, Any],
) -> str:
    lines: list[str] = []
    role = str(task_envelope.get("task_owner") or spec_name or "").strip()
    if role:
        lines.append(f"Role: {redact_text(role)}")
    mission = str(
        task_envelope.get("__team_task")
        or task_envelope.get("task_subject")
        or ""
    ).strip()
    if mission:
        lines.append(f"Mission: {redact_text(mission)}")
    original = str(
        task_envelope.get("original_user_prompt")
        or task_envelope.get("original_user_request")
        or ""
    ).strip()
    if original and original != mission:
        lines.append(f"Original user request: {redact_text(original)}")
    instructions = str(task_envelope.get("__team_instructions") or "").strip()
    if instructions:
        lines.append(f"Role instructions: {redact_text(instructions)}")
    open_work_items = task_envelope.get("open_work_items")
    if isinstance(open_work_items, list) and open_work_items:
        lines.append("Open parent work items:")
        for idx, item in enumerate(open_work_items[:12], start=1):
            if isinstance(item, dict):
                content = str(
                    item.get("content")
                    or item.get("activeForm")
                    or item.get("active_form")
                    or ""
                ).strip()
                status = str(item.get("status") or "pending").strip()
            else:
                content = str(item or "").strip()
                status = "pending"
            if not content:
                continue
            lines.append(f"{idx}. [{redact_text(status)}] {redact_text(content)}")
    requirements = task_envelope.get("research_requirements")
    if isinstance(requirements, dict):
        policy = str(requirements.get("policy") or "").strip()
        if policy:
            lines.append(f"Requirement policy: {redact_text(policy)}")
    return "\n".join(lines).strip()


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
    # Required execution dependencies; no direct-handler or second-engine fallback.
    tool_registry: Any
    tool_executor: Any

    # ---------------------------------------------------------------- config
    def _execution_limit(
        self, spec: SubAgentSpec | None, name: str, default: int | float,
        *, minimum: float = 0,
    ) -> int | float:
        value = getattr(getattr(spec, "execution_policy", None), name, None)
        if value is None:
            value = self.config.get(f"agent.subagents.{name}", default)
        try:
            value = type(default)(default if value is None else value)
            if not math.isfinite(value) or value < minimum:
                raise ValueError
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"agent.subagents.{name} must be finite and >= {minimum}") from exc
        return value

    def _max_iterations(self, spec: SubAgentSpec | None = None) -> int:
        return int(self._execution_limit(spec, "max_iterations", 60, minimum=1))

    def _max_skill_calls(self, spec: SubAgentSpec | None = None) -> int:
        return int(self._execution_limit(spec, "max_skill_calls", 120))

    def _max_wall_seconds(self, spec: SubAgentSpec | None = None) -> float:
        return float(self._execution_limit(spec, "max_wall_seconds", 600.0))

    def _llm_max_attempts(self, spec: SubAgentSpec | None = None) -> int:
        return int(self._execution_limit(spec, "llm_max_attempts", 2, minimum=1))

    def _max_extra_llm_attempts(self) -> int:
        configured = self.config.get(
            "agent.subagents.max_extra_llm_attempts_per_run",
            DEFAULT_EXTRA_ATTEMPT_LIMIT,
        )
        try:
            return max(0, int(configured))
        except (TypeError, ValueError):
            return DEFAULT_EXTRA_ATTEMPT_LIMIT

    def _model_override(self, spec: SubAgentSpec) -> tuple[str | None, str | None]:
        """Return a policy-approved per-role provider/model override.

        ``tier_routes`` lets a locked role select among provider/model pairs
        already assigned to its tier, without allowing an expensive route to
        masquerade as ``light``. Other roles retain unrestricted overrides.
        """

        provider = str(spec.provider or "").strip().lower()
        model = str(spec.model or "").strip()
        if not provider and not model:
            return None, None
        policy = spec.execution_policy
        if not policy.allow_model_override or policy.model_override_scope == "none":
            return None, None
        if policy.model_override_scope != "tier_routes":
            return provider or None, model or None

        tier_cfg = self.config.get(f"llm.tiers.{spec.tier}", {}) or {}
        if not isinstance(tier_cfg, dict):
            return None, None
        matches: list[tuple[str, str]] = []
        for route in configured_routes(tier_cfg):
            route_provider = str(route.get("provider") or "").strip().lower()
            for route_model in configured_models(route):
                candidate_model = str(route_model or "").strip()
                if provider and provider != route_provider:
                    continue
                if model and model != candidate_model:
                    continue
                matches.append((route_provider, candidate_model))
        if not matches:
            return None, None
        matched_provider, matched_model = matches[0]
        return matched_provider or None, matched_model or None

    def _finalization_reserve_seconds(self) -> float:
        return float(self._execution_limit(
            None, "finalization_reserve_seconds", float(SUBAGENT_FINALIZATION_RESERVE_SECONDS),
        ))

    def _preloaded_skill_context(self, spec: SubAgentSpec) -> str:
        """Load only skill bodies selected by declarative role policy."""

        selected = list(spec.execution_policy.preload_skills or [])
        if not selected:
            return ""
        blocks: list[str] = []
        registry = getattr(self.skills, "registry", None)
        if registry is None:
            return ""
        for skill_id in selected:
            try:
                entry = registry.get(skill_id)
            except Exception:
                continue
            manifest = getattr(entry, "manifest", None)
            instructions = str(
                getattr(manifest, "instructions", "") or ""
            ).strip()
            if instructions:
                blocks.append(f"-- skill:{skill_id} --\n{instructions}")
        return "\n\n".join(blocks)

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
        context_scope: SubAgentContextScope = DEFAULT_CONTEXT_SCOPE,
        delegation_depth: int = 0,
        cancel_token: Any = None,
        max_wall_seconds: float | None = None,
        completion_gate: CompletionGateLike | None = None,
    ) -> dict[str, Any]:
        """Run a child on the canonical messages -> tools loop."""

        from ..agent.loop import WorkspaceNativeAgentLoop
        from ..agent.loop_contracts import LoopConfig
        from ..tools.orchestrator import ToolOrchestrator

        if context_scope not in {
            DEFAULT_CONTEXT_SCOPE,
            EXPLICIT_PAYLOAD_ONLY_CONTEXT_SCOPE,
        }:
            raise ValueError(f"unknown subagent context scope: {context_scope!r}")
        if self.tool_registry is None or self.tool_executor is None:
            raise RuntimeError(
                "native subagent runtime requires the parent tool registry and executor"
            )

        t_start = time.monotonic()
        explicit_payload_only = context_scope == EXPLICIT_PAYLOAD_ONLY_CONTEXT_SCOPE
        task_envelope, data_payload = _split_task_payload(payload)
        if explicit_payload_only:
            allowed_native_tools: list[str] = []
            base_context = ""
        else:
            allowed_native_tools = self._allowed_native_tool_names(
                spec=spec,
                delegation_depth=delegation_depth,
            )
            base_context = self._preloaded_skill_context(spec)

        max_calls = self._max_skill_calls(spec)
        if max_calls <= 0:
            allowed_native_tools = []
        required_native_tools = list(spec.execution_policy.required_native_tools)
        unavailable = set(required_native_tools) - set(allowed_native_tools)
        if unavailable:
            raise ValueError(f"required tools unavailable in this execution scope: {sorted(unavailable)}")
        safe_payload = redact_display_dict(data_payload)
        safe_task_envelope = redact_display_dict(task_envelope)
        prompt = self._render_prompt(
            spec, data_payload, base_context,
            task_envelope=task_envelope, context_scope=context_scope,
        )
        audit_prompt = redact_text(self._render_prompt(
            spec, safe_payload, base_context,
            task_envelope=safe_task_envelope, context_scope=context_scope,
        ))

        try:
            from ..agent.streaming import get_default_bus

            bus = get_default_bus()
        except Exception:
            bus = None
        event_fields = {
            "turn_id": turn_id,
            "team_run_id": task_envelope.get("team_run_id"),
            "team_template": task_envelope.get("team_template"),
            "team_call_id": task_envelope.get("team_call_id") or parent_call_id,
            "team_task_id": task_envelope.get("task_id"),
            "team_task_owner": task_envelope.get("task_owner"),
            "team_task_subject": task_envelope.get("task_subject")
            or task_envelope.get("__team_task"),
        }

        def _publish(kind: str, **fields: Any) -> None:
            if bus is None:
                return
            try:
                bus.publish(
                    kind,
                    subagent=spec.name,
                    tier=spec.tier,
                    strategy_id=strategy_id,
                    session_id=session_id,
                    trigger_event_id=trigger_event_id,
                    **{k: v for k, v in event_fields.items() if v is not None},
                    **fields,
                )
            except Exception:
                pass

        audit_start = {
            "subagent": spec.name,
            "tier": spec.tier,
            "prompt_path": str(spec.prompt_path) if spec.prompt_path else "",
            "role_prompt": redact_text(spec.prompt or ""),
            "payload": safe_payload,
            "payload_keys": sorted(data_payload.keys()),
            "task_envelope": safe_task_envelope,
            "task_envelope_keys": sorted(task_envelope.keys()),
            "allowed_skills": list(spec.allowed_skills or []),
            "callable_skills": [],
            "native_tools": list(allowed_native_tools),
            "context_chars": len(base_context or ""),
            "context_scope": context_scope,
            "runtime": "native",
            "redacted": True,
        }
        _publish(
            "subagent.start",
            payload_keys=audit_start["payload_keys"],
            payload=audit_start["payload"],
            task_envelope_keys=audit_start["task_envelope_keys"],
            task_envelope=audit_start["task_envelope"],
            role_prompt=audit_start["role_prompt"],
            prompt_path=audit_start["prompt_path"],
            allowed_skills=audit_start["allowed_skills"],
            callable_skills=[],
            native_tools=audit_start["native_tools"],
            context_chars=audit_start["context_chars"],
            runtime="native",
        )
        _publish(
            "subagent.step",
            step_kind="prompt",
            iteration=0,
            status="sent",
            prompt=redact_text(audit_prompt),
            prompt_chars=len(audit_prompt),
            payload=safe_payload,
            runtime="native",
        )

        tier_config = self.config.get(f"llm.tiers.{spec.tier}", {}) or {}
        if not isinstance(tier_config, dict):
            tier_config = {}
        configured_wall = self._max_wall_seconds(spec)
        if max_wall_seconds is not None:
            supplied_wall = float(max_wall_seconds)
            if not math.isfinite(supplied_wall) or supplied_wall < 0:
                raise ValueError("max_wall_seconds must be finite and non-negative")
            configured_wall = min(configured_wall, supplied_wall)
        model_provider, model_id = self._model_override(spec)
        tool_metadata = {
            "subagent": spec.name,
            "parent_call_id": parent_call_id,
            "delegation_depth": max(0, int(delegation_depth or 0)),
            "context_scope": context_scope,
            **event_fields,
        }
        loop_config = LoopConfig.from_config(
            self.config, turn_id=turn_id,
            max_iterations=self._max_iterations(spec),
            tier=spec.tier,
            task="subagent_analysis",
            caller=f"subagent:{spec.name}",
            reasoning_effort=str(tier_config.get("reasoning_effort") or "") or None,
            reasoning_summary=str(tier_config.get("reasoning_summary") or "") or None,
            model_provider=model_provider,
            model_id=model_id,
            session_id=session_id,
            strategy_id=strategy_id,
            trigger_event_id=trigger_event_id,
            max_wall_seconds=configured_wall,
            max_total_tool_calls=max_calls,
            wall_time_final_synthesis_seconds=min(
                self._finalization_reserve_seconds(),
                max(1.0, configured_wall / 2),
            ),
            llm_retry_attempts=self._llm_max_attempts(spec),
            max_extra_llm_attempts_per_turn=self._max_extra_llm_attempts(),
            required_artifacts=tuple(
                {"tool": name} for name in required_native_tools
            ),
            workspace_root=str(getattr(self.config.paths, "root", "") or ""),
            tool_argument_defaults={
                name: dict(spec.execution_policy.tool_argument_defaults.get(name) or {})
                for name in allowed_native_tools
                if name in spec.execution_policy.tool_argument_defaults
            },
            tool_call_metadata=tool_metadata,
        )
        allowed_set = frozenset(allowed_native_tools)

        def _tool_filter(descriptor: Any) -> bool:
            return str(getattr(descriptor, "name", "") or "") in allowed_set

        def _event_sink(envelope: Any) -> None:
            block = getattr(envelope, "block", None)
            if not isinstance(block, dict):
                return
            kind = str(block.get("kind") or "")
            iteration = int(block.get("index") or 0)
            if kind == "tool_use":
                _publish(
                    "subagent.step",
                    step_kind="act",
                    iteration=iteration,
                    status="started",
                    skill=block.get("action") or block.get("skill_id"),
                    action="(native)",
                    runtime="native",
                )
            elif kind == "tool_result":
                _publish(
                    "subagent.step",
                    step_kind="observe",
                    iteration=iteration,
                    status="ok" if block.get("ok") else "error",
                    skill=block.get("action") or block.get("skill_id"),
                    action="(native)",
                    error=block.get("error"),
                    runtime="native",
                )

        from ..tools.registry import ToolRegistry
        execution_registry = ToolRegistry() if explicit_payload_only else self.tool_registry
        loop = WorkspaceNativeAgentLoop(
            gateway=self.llm,
            registry=execution_registry,
            orchestrator=ToolOrchestrator(
                registry=execution_registry,
                executor=self.tool_executor,
                max_parallel=int(self.config.get("agent.native.max_parallel", 4) or 4),
            ),
            config=loop_config,
            event_sink=_event_sink,
        )
        outcome = loop.run(
            system=(
                "You are a delegated Nerya subagent. Follow the role, task, "
                "and evidence contract in the user message. Use native tools "
                "only when they are provided."
            ),
            user_message=prompt,
            tool_filter=_tool_filter,
            cancel_token=cancel_token,
            turn_id=turn_id,
            completion_gate=completion_gate,
        )

        skill_calls, rejected_actions = _native_tool_records(
            outcome.blocks,
            caller=f"subagent:{spec.name}",
        )
        cancelled = _token_is_set(cancel_token) or (
            outcome.stop_reason == "cancelled" and outcome.aborted
        )
        close_reason = (
            _token_reason(cancel_token)
            if cancelled and _token_is_set(cancel_token)
            else str(outcome.stop_reason or outcome.transition_reason or "end_turn")
        )
        final_output = _native_final_output(
            outcome.final_text,
            stop_reason=outcome.stop_reason,
        )
        if cancelled:
            final_output.update({
                "done": True,
                "cancelled": True,
                "error_kind": "cancelled",
                "summary": f"subagent cancelled: {close_reason}",
            })
        final_output.setdefault("role", spec.name)
        if outcome.aborted:
            final_output.update(done=False, degraded=True)
        if outcome.completion_status == "blocked":
            final_output.update(done=False, degraded=True,
                error_kind=outcome.completion_reason or outcome.stop_reason)
        signals_used: list[str] = []
        for signal in _coerce_list(
            final_output.get("signals") or final_output.get("signals_used")
        ):
            if str(signal) not in signals_used:
                signals_used.append(str(signal))
        evidence = [
            item if isinstance(item, dict) else {"note": str(item)}
            for item in _coerce_list(final_output.get("evidence"))
        ]
        try:
            uncertainty = max(
                0.0,
                min(1.0, float(final_output.get("uncertainty") or 0.0)),
            )
        except (TypeError, ValueError):
            uncertainty = 0.0
        total_tokens = max(
            0,
            int(outcome.input_tokens_total or 0)
            + int(outcome.output_tokens_total or 0),
        )
        model_calls = []
        for call in outcome.model_calls:
            row = dict(call)
            row["tier"] = spec.tier
            row["tokens"] = max(
                0,
                int(row.get("input_tokens") or 0)
                + int(row.get("output_tokens") or 0),
            )
            row["usd"] = float(row.get("usd") or 0.0)
            model_calls.append(row)
        steps = [
            _StepRecord(
                kind="prompt",
                iteration=0,
                status="sent",
                detail={"prompt_chars": len(audit_prompt)},
            )
        ]
        for call in model_calls:
            steps.append(_StepRecord(
                kind="think",
                iteration=int(call.get("iteration") or 0),
                status="ok",
                tokens=int(call.get("tokens") or 0),
                usd=float(call.get("usd") or 0.0),
                detail={
                    "provider": call.get("provider"),
                    "model": call.get("model"),
                },
            ))
        for envelope in outcome.blocks:
            block = getattr(envelope, "block", None)
            if not isinstance(block, dict):
                continue
            kind = str(block.get("kind") or "")
            if kind == "tool_use":
                steps.append(_StepRecord(
                    kind="act",
                    iteration=int(block.get("index") or 0),
                    status="ok",
                    detail={"skill": block.get("action") or block.get("skill_id")},
                ))
            elif kind == "tool_result":
                steps.append(_StepRecord(
                    kind="observe",
                    iteration=int(block.get("index") or 0),
                    status="ok" if block.get("ok") else "error",
                    detail={"skill": block.get("action") or block.get("skill_id")},
                    error=str(block.get("error") or "") or None,
                ))
        steps.append(_StepRecord(
            kind="close",
            iteration=int(outcome.iterations or 0),
            status="cancelled" if cancelled else "ok",
            detail={"close_reason": close_reason},
            tokens=total_tokens,
            usd=float(outcome.usd_total or 0.0),
            wall_ms=int((time.monotonic() - t_start) * 1000),
            error=close_reason if cancelled else None,
        ))
        contribution_metrics = {
            "signals_used": signals_used,
            "skill_calls": skill_calls,
            "rejected_actions": rejected_actions,
            "uncertainty": uncertainty,
            "evidence": evidence,
            "attempt_budget": {
                "limit": int(outcome.extra_llm_attempt_limit or 0),
                "used": int(outcome.extra_llm_attempts or 0),
                "remaining": max(
                    0,
                    int(outcome.extra_llm_attempt_limit or 0)
                    - int(outcome.extra_llm_attempts or 0),
                ),
                "by_reason": dict(outcome.extra_llm_attempts_by_reason),
            },
        }
        _publish(
            "subagent.step",
            step_kind="close",
            iteration=int(outcome.iterations or 0),
            wall_ms=int((time.monotonic() - t_start) * 1000),
            iterations=int(outcome.iterations or 0),
            skill_calls_n=len(skill_calls),
            rejected_actions_n=len(rejected_actions),
            tokens=total_tokens,
            usd=float(outcome.usd_total or 0.0),
            close_reason=close_reason,
            runtime="native",
        )
        audit = {
            **audit_start,
            "prompt_records": [{
                "iteration": 0,
                "prompt": redact_text(audit_prompt),
                "prompt_chars": len(audit_prompt),
                "redacted": True,
            }],
            "provider": outcome.provider,
            "model": outcome.model,
            "model_calls": model_calls,
            "redacted": True,
        }
        _publish(
            "subagent.end",
            iterations=int(outcome.iterations or 0),
            skill_calls=len(skill_calls),
            rejected=len(rejected_actions),
            tokens=total_tokens,
            usd=float(outcome.usd_total or 0.0),
            wall_ms=int((time.monotonic() - t_start) * 1000),
            output=redact_display_dict(final_output),
            metrics=redact_display_dict(contribution_metrics),
            close_reason=close_reason,
            runtime="native",
        )
        return {
            "subagent": spec.name,
            "tier": spec.tier,
            "provider": str(outcome.provider or ""),
            "model": str(outcome.model or ""),
            "model_calls": model_calls,
            "output": final_output,
            "cancelled": cancelled,
            "close_reason": close_reason,
            "tokens": total_tokens,
            "usd": float(outcome.usd_total or 0.0),
            "metrics": {**contribution_metrics, "iterations": int(outcome.iterations or 0)},
            "steps": [step.asdict() for step in steps],
            "audit": audit,
            "completion_status": (
                "blocked" if outcome.aborted or final_output.get("degraded")
                or final_output.get("done") is False else outcome.completion_status
            ),
            "completion_rounds": outcome.completion_rounds,
            "completion": {
                "status": outcome.completion_status,
                "reason": outcome.completion_reason,
                "feedback": outcome.completion_feedback,
            },
        }

    def _render_prompt(
        self,
        spec: SubAgentSpec,
        payload: dict[str, Any],
        context: str,
        *,
        task_envelope: dict[str, Any],
        context_scope: SubAgentContextScope,
    ) -> str:
        """Task context only; the provider tool API owns executable contracts."""
        assignment = _render_subagent_task_assignment(
            spec_name=spec.name, task_envelope=task_envelope,
        )
        language = task_envelope.get("analysis_language") or payload.get("analysis_language")
        output_language = task_envelope.get("output_language") or payload.get("output_language")
        instructions = (
            "Use only the native tools supplied through the tool API. "
            "Choose and revise your approach from observed results within the caller's budget. "
            "When evidence is insufficient, try a different permitted source or method. "
            "Do not blindly repeat mutations. Report remaining blockers honestly. "
            "Return one JSON object with a summary, evidence references, and done=true only "
            "when the assignment is satisfied. Tool calls belong in the tool API, never in prose."
        )
        if context_scope == EXPLICIT_PAYLOAD_ONLY_CONTEXT_SCOPE:
            instructions = (
                "This run is isolated to the explicit task payload. No tools are available. "
                "Do not rely on chat history, global memory, operator profile, or other sessions. "
                "Return the requested analysis and explicitly label gaps in the supplied evidence."
            )
        return "\n\n".join(part for part in (
            f"You are the {spec.name} subagent.\n{spec.prompt}",
            "=== session facts ===\n"
            f"Current datetime (UTC): {now_iso()}\n"
            "Ground factual and numeric claims in this run's tool results or supplied evidence. "
            "Never present remembered market values as current or simulated orders as live fills.",
            f"=== team assignment ===\n{assignment}" if assignment else "",
            f"=== analysis language ===\n{language}" if language else "",
            f"=== output language ===\n{output_language}" if output_language else "",
            "=== task payload ===\n" + wrap_untrusted(
                "payload", json.dumps(payload, ensure_ascii=False, default=str),
            ),
            f"=== context ===\n{context}" if context else "",
            instructions,
        ) if part)

    def _allowed_native_tool_names(
        self,
        *,
        spec: SubAgentSpec | None = None,
        delegation_depth: int = 0,
    ) -> list[str]:
        """Return the subset of parent native tools children may invoke.

        The child inherits the parent's native-tool surface
        (connector_list / connector_view / memory_* / recipe_view /
        read / glob / grep / search / shell …) so it can self-discover
        venues mid-run. The destructive surface (live trading writes,
        evolve_promote, subagent_run) and any DANGEROUS-tier tool stays
        parent-only — the dispatcher itself
        plus :data:`CHILD_NATIVE_TOOL_DENYLIST_PREFIXES` enforce that.

        Role-specific visibility comes from ``SubAgentExecutionPolicy``.
        Delegating tools declare their own depth ceiling on the descriptor,
        so nested fan-out is bounded without payload markers or tool-name
        branches in this runtime.
        """

        registry = self.tool_registry
        if registry is None:
            return []
        if self.tool_executor is None:
            return []
        policy = SubAgentExecutionPolicy.from_dict(
            getattr(spec, "execution_policy", None),
        )
        allow = None if policy.native_tool_allow is None else set(policy.native_tool_allow)
        deny = set(policy.native_tool_deny)
        try:
            current_depth = max(0, int(delegation_depth))
        except (TypeError, ValueError):
            current_depth = 0
        out: list[str] = []
        for descriptor in registry.list_tools():
            name = str(getattr(descriptor, "name", "") or "")
            if not name:
                continue
            if any(name.startswith(p) for p in CHILD_NATIVE_TOOL_DENYLIST_PREFIXES):
                continue
            if allow is not None and name not in allow:
                continue
            if name in deny:
                continue
            max_depth = getattr(descriptor, "child_max_depth", None)
            if max_depth is not None:
                try:
                    if current_depth >= int(max_depth):
                        continue
                except (TypeError, ValueError):
                    continue
            risk = str(getattr(getattr(descriptor, "risk", None), "value", "") or "")
            if risk and risk.lower() in CHILD_NATIVE_TOOL_DENY_RISK:
                continue
            out.append(name)
        return sorted(out)


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
