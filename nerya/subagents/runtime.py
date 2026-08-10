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
from typing import Any, Literal

from ..core.config import Config
from ..core.errors import (
    LLMApprovalRequired,
    LLMError,
    LLMScriptQuotaExceeded,
    LLMStructuredOutputError,
    LLMTaskNotAllowed,
    LLMTierDenied,
)
from ..core.redaction import redact_display_dict, redact_text
from ..core.time import now_iso
from ..llm.gateway import LLMGateway
from ..llm.route_candidates import configured_models, configured_routes
from ..security.prompt_injection import wrap_untrusted
from ..skills.kernel import SkillKernel
from ..agent.runtime import (
    AgentRuntime,
    CompletionGateLike,
    RuntimeRequest,
    TurnSnapshot,
)
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


def _token_is_set(token: Any) -> bool:
    if token is None:
        return False
    state = getattr(token, "is_set", False)
    try:
        return bool(state() if callable(state) else state)
    except Exception:
        return True


def _token_reason(token: Any) -> str:
    return str(getattr(token, "reason", "") or "cancelled")


def _spec_profile_name(spec: SubAgentSpec | None) -> str:
    if spec is None:
        return ""
    return str(getattr(spec, "canonical_name", None) or spec.name or "")


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

class SubAgentLLMError(RuntimeError):
    """Raised when a child runtime cannot produce any model output."""


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


def _append_completion_feedback_payload(
    payload: dict[str, Any],
    feedback: str,
) -> dict[str, Any]:
    """Copy a child payload before adding caller-gate feedback."""

    clean = str(feedback or "").strip()
    if not clean:
        return dict(payload or {})
    updated = dict(payload or {})
    updated["__completion_gate_feedback"] = clean
    return updated


def _native_tool_records(
    blocks: list[Any],
    *,
    caller: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project canonical loop blocks onto the existing child metrics shape."""

    payload_by_call_id: dict[str, Any] = {}
    successful: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for envelope in blocks or []:
        block = getattr(envelope, "block", None)
        if not isinstance(block, dict) and isinstance(envelope, dict):
            block = envelope.get("block", envelope)
        if not isinstance(block, dict):
            continue
        call_id = str(block.get("call_id") or "")
        if block.get("kind") == "tool_use":
            if call_id:
                payload_by_call_id[call_id] = block.get("payload") or {}
            continue
        if block.get("kind") != "tool_result":
            continue
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
        rejected.append(record)
    return successful, rejected


def _native_result_record(
    value: Any,
    *,
    tool_name: str,
    ok: bool,
) -> dict[str, Any]:
    """Restore the legacy structured result contract from loop blocks."""

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
            "summary": "subagent finished without visible final output",
            "raw": "",
            "degraded": True,
            "error_kind": "empty_model_output",
        }
    output.setdefault("done", stop_reason == "end_turn")
    if stop_reason != "end_turn":
        output.setdefault("degraded", True)
        output.setdefault("error_kind", stop_reason or "incomplete")
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


_NON_RETRYABLE_SUBAGENT_LLM_ERRORS: tuple[type[Exception], ...] = (
    LLMTierDenied,
    LLMTaskNotAllowed,
    LLMScriptQuotaExceeded,
    LLMStructuredOutputError,
    LLMApprovalRequired,
)

_TRANSIENT_SUBAGENT_LLM_HINTS: tuple[str, ...] = (
    "(429)",
    "(500)",
    "(502)",
    "(503)",
    "(504)",
    "(522)",
    "(524)",
    "(529)",
    "rate_limit",
    "rate-limit",
    "rate limit",
    "timeout",
    "timed out",
    "connection",
    "network",
    "unreachable",
    "temporarily unavailable",
    "temporarily busy",
    "server busy",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "remote end closed connection",
    "服务器短暂繁忙",
    "短暂繁忙",
    "稍后重试",
    "ECONN",
    "ETIMEDOUT",
    "EAI_AGAIN",
)


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
    # Parent-owned executor shared with the root turn. When present, every
    # native child call goes through its schema/permission/approval/risk
    # pipeline instead of invoking a descriptor handler directly.
    tool_executor: Any = None
    # Dispatcher-created runtimes fail closed when a native registry is
    # present but the parent forgot to provide its executor. Directly-created
    # legacy runtimes leave this false for backwards-compatible test/ad-hoc
    # integrations; the production dispatcher always sets it from its
    # registry wiring.
    require_tool_executor: bool = False

    # ---------------------------------------------------------------- config
    def _max_iterations(self, spec: SubAgentSpec | None = None) -> int:
        policy_value = getattr(
            getattr(spec, "execution_policy", None),
            "max_iterations",
            None,
        )
        if policy_value is not None:
            return max(1, int(policy_value))
        explicit = self.config.get("agent.subagents.max_iterations", None)
        if explicit is not None:
            return max(1, int(explicit))
        return 60

    def _max_skill_calls(self, spec: SubAgentSpec | None = None) -> int:
        policy_value = getattr(
            getattr(spec, "execution_policy", None),
            "max_skill_calls",
            None,
        )
        if policy_value is not None:
            return max(0, int(policy_value))
        configured = self.config.get("agent.subagents.max_skill_calls", 120)
        return max(0, int(120 if configured is None else configured))

    def _max_wall_seconds(self, spec: SubAgentSpec | None = None) -> float:
        policy_value = getattr(
            getattr(spec, "execution_policy", None),
            "max_wall_seconds",
            None,
        )
        if policy_value is not None:
            return max(5.0, float(policy_value))
        explicit = self.config.get("agent.subagents.max_wall_seconds", None)
        if explicit is not None:
            try:
                return max(5.0, float(explicit))
            except (TypeError, ValueError):
                return 120.0
        return 600.0

    def _llm_max_attempts(self, spec: SubAgentSpec | None = None) -> int:
        policy_value = getattr(
            getattr(spec, "execution_policy", None),
            "llm_max_attempts",
            None,
        )
        if policy_value is not None:
            return max(1, int(policy_value))
        configured = self.config.get("agent.subagents.llm_max_attempts", 2)
        return max(1, int(2 if configured is None else configured))

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
        configured = self.config.get(
            "agent.subagents.finalization_reserve_seconds",
            SUBAGENT_FINALIZATION_RESERVE_SECONDS,
        )
        try:
            return max(
                5.0,
                float(
                    SUBAGENT_FINALIZATION_RESERVE_SECONDS
                    if configured is None
                    else configured
                ),
            )
        except (TypeError, ValueError):
            return SUBAGENT_FINALIZATION_RESERVE_SECONDS

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
    @staticmethod
    def _resolve_runtime_mode(spec: SubAgentSpec, requested: str) -> str:
        mode = str(requested or "auto").strip().lower()
        if mode not in {"auto", "legacy", "native"}:
            raise ValueError(f"unknown subagent runtime mode: {mode!r}")
        if mode == "auto":
            mode = str(spec.execution_policy.runtime or "legacy")
        return mode

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
        completion_gate: CompletionGateLike | None = None,
        cancel_token: Any = None,
        max_wall_seconds: float | None = None,
        runtime_mode: str = "auto",
    ) -> dict[str, Any]:
        """Run one child through the selected engine and shared lifecycle."""

        selected_runtime = self._resolve_runtime_mode(spec, runtime_mode)

        def _run_once(next_payload: dict[str, Any], wall_seconds: float | None):
            if selected_runtime == "native":
                return self._run_native(
                    spec,
                    trigger_event_id=trigger_event_id,
                    payload=next_payload,
                    strategy_id=strategy_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    parent_call_id=parent_call_id,
                    context_scope=context_scope,
                    delegation_depth=delegation_depth,
                    cancel_token=cancel_token,
                    max_wall_seconds=wall_seconds,
                )
            return self._run_legacy(
                spec,
                trigger_event_id=trigger_event_id,
                payload=next_payload,
                strategy_id=strategy_id,
                session_id=session_id,
                turn_id=turn_id,
                parent_call_id=parent_call_id,
                context_scope=context_scope,
                delegation_depth=delegation_depth,
                cancel_token=cancel_token,
                max_wall_seconds=wall_seconds,
            )

        continuation_started = time.monotonic()
        continuation_wall_seconds: float | None = None
        runtime_config = getattr(self, "config", None)
        if completion_gate is not None and runtime_config is not None:
            continuation_wall_seconds = self._max_wall_seconds(spec)
            if max_wall_seconds is not None:
                try:
                    continuation_wall_seconds = min(
                        continuation_wall_seconds,
                        max(0.0, float(max_wall_seconds)),
                    )
                except (TypeError, ValueError):
                    pass

        if completion_gate is None:
            return _run_once(payload, max_wall_seconds)

        def _execute(feedback: str) -> dict[str, Any]:
            remaining = (
                max(
                    0.0,
                    continuation_wall_seconds
                    - (time.monotonic() - continuation_started),
                )
                if continuation_wall_seconds is not None
                else None
            )
            return _run_once(
                _append_completion_feedback_payload(payload, feedback),
                remaining,
            )

        def _snapshot(output: dict[str, Any], round_index: int) -> TurnSnapshot:
            metrics = output.get("metrics") if isinstance(output, dict) else {}
            metrics = metrics if isinstance(metrics, dict) else {}
            skill_calls = metrics.get("skill_calls") or []
            rejected = metrics.get("rejected_actions") or []
            return TurnSnapshot(
                iteration=round_index,
                transcript=tuple(
                    (record.get("prompt") for record in (output.get("audit") or {}).get("prompt_records", []))
                    if isinstance(output, dict)
                    else ()
                ),
                tool_results=tuple(
                    [*skill_calls, *rejected]
                    if isinstance(skill_calls, list) and isinstance(rejected, list)
                    else ()
                ),
                output=(output.get("output") if isinstance(output, dict) else output),
                stop_reason=str(
                    (output.get("close_reason") if isinstance(output, dict) else "")
                    or ""
                ),
                usage={
                    "tokens": int((output or {}).get("tokens", 0) or 0)
                    if isinstance(output, dict) else 0,
                    "usd": float((output or {}).get("usd", 0.0) or 0.0)
                    if isinstance(output, dict) else 0.0,
                },
                metadata={
                    "runtime": "subagent",
                    "subagent": spec.name,
                    "turn_id": turn_id or "",
                    "strategy_id": strategy_id or "",
                },
            )

        max_rounds = max(1, int(getattr(completion_gate, "max_rounds", 2) or 2))
        if runtime_config is not None:
            max_rounds = min(max_rounds, self._max_iterations(spec))
            configured_wall = self._max_wall_seconds(spec)
            if max_wall_seconds is not None:
                try:
                    configured_wall = min(
                        configured_wall,
                        max(0.0, float(max_wall_seconds)),
                    )
                except (TypeError, ValueError):
                    configured_wall = self._max_wall_seconds(spec)
            max_wall_seconds = configured_wall
        else:
            # Lightweight compatibility fixtures may construct the runtime
            # with ``__new__`` and only replace the legacy runner.
            max_wall_seconds = None
        shared = AgentRuntime[dict[str, Any]]()
        result = shared.run(
            RuntimeRequest(
                max_rounds=max_rounds,
                max_wall_seconds=max_wall_seconds,
                cancel=cancel_token,
            ),
            completion_gate,
            execute=_execute,
            snapshot=_snapshot,
        )
        output = result.value or {
            "subagent": spec.name,
            "output": {},
            "metrics": {},
            "steps": [],
            "audit": {},
        }
        output["completion"] = result.decision.asdict()
        output["completion_rounds"] = result.rounds
        if result.decision.status == "blocked":
            final_output = output.get("output")
            if not isinstance(final_output, dict):
                final_output = {}
            final_output = dict(final_output)
            if result.decision.reason == "cancelled":
                output["cancelled"] = True
                output["close_reason"] = "cancelled"
                final_output.update({
                    "done": True,
                    "degraded": True,
                    "cancelled": True,
                    "error_kind": "cancelled",
                    "summary": "subagent cancelled before the next round",
                })
            else:
                final_output.update({
                    "done": True,
                    "degraded": True,
                    "error_kind": "completion_gate_blocked",
                    "summary": (
                        "caller completion gate blocked this subagent result: "
                        f"{result.decision.reason or 'blocked'}"
                    ),
                })
            output["output"] = final_output
            output["completion_status"] = "blocked"
        else:
            output["completion_status"] = result.decision.status
        return output

    def _run_native(
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
    ) -> dict[str, Any]:
        """Run a child on the canonical messages -> tools loop."""

        from ..agent.loop import LoopConfig, WorkspaceNativeAgentLoop
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
            preloaded: list[str] = []
            base_context = ""
        else:
            allowed_native_tools = self._allowed_native_tool_names(
                spec=spec,
                delegation_depth=delegation_depth,
            )
            preloaded = [
                skill
                for skill in (*spec.allowed_skills, *CHILD_CORE_SELF_CONTROL_SKILLS)
                if skill not in CHILD_SKILL_DENYLIST
            ]
            base_context = self._preloaded_skill_context(spec)

        max_calls = self._max_skill_calls(spec)
        if max_calls <= 0:
            allowed_native_tools = []
        required_native_tools = [
            name
            for name in spec.execution_policy.required_native_tools
            if name in allowed_native_tools
        ]
        safe_payload = redact_display_dict(data_payload)
        safe_task_envelope = redact_display_dict(task_envelope)
        prompt = self._render_prompt(
            spec,
            data_payload,
            base_context,
            [],
            allowed=preloaded,
            native_tools=allowed_native_tools,
            task_envelope=task_envelope,
            context_scope=context_scope,
            native_protocol=True,
        )
        audit_prompt = self._render_prompt(
            spec,
            safe_payload,
            base_context,
            [],
            allowed=preloaded,
            native_tools=allowed_native_tools,
            task_envelope=safe_task_envelope,
            context_scope=context_scope,
            native_protocol=True,
        )

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
            try:
                configured_wall = min(configured_wall, max(0.0, float(max_wall_seconds)))
            except (TypeError, ValueError):
                pass
        model_provider, model_id = self._model_override(spec)
        try:
            max_tokens = int(
                self.config.get(
                    "agent.native.max_tokens",
                    tier_config.get("max_tokens", 4096),
                )
                or 4096
            )
        except (TypeError, ValueError):
            max_tokens = 4096
        try:
            temperature = float(
                self.config.get(
                    "agent.native.temperature",
                    tier_config.get("temperature", 0.2),
                )
                or 0.0
            )
        except (TypeError, ValueError):
            temperature = 0.2
        tool_metadata = {
            "subagent": spec.name,
            "parent_call_id": parent_call_id,
            "delegation_depth": max(0, int(delegation_depth or 0)),
            "context_scope": context_scope,
            **event_fields,
        }
        loop_config = LoopConfig(
            turn_id=turn_id,
            max_iterations=self._max_iterations(spec),
            max_tokens=max_tokens,
            temperature=temperature,
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
            max_total_tool_calls=max_calls if max_calls > 0 else None,
            wall_time_final_synthesis_seconds=min(
                self._finalization_reserve_seconds(),
                max(1.0, configured_wall / 2),
            ),
            llm_retry_attempts=self._llm_max_attempts(spec),
            token_budget=(
                int(self.config.get("agent.native.token_budget", 0) or 0) or None
            ),
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

        loop = WorkspaceNativeAgentLoop(
            gateway=self.llm,
            registry=self.tool_registry,
            orchestrator=ToolOrchestrator(
                registry=self.tool_registry,
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
        final_output = _attach_data_coverage(
            final_output,
            requested_role=spec.name,
            role_profile=_spec_profile_name(spec),
            skill_calls=skill_calls,
            rejected_actions=rejected_actions,
        )
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
        }

    def _run_legacy(
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
    ) -> dict[str, Any]:
        t_start = time.monotonic()
        steps: list[_StepRecord] = []
        if context_scope not in {
            DEFAULT_CONTEXT_SCOPE,
            EXPLICIT_PAYLOAD_ONLY_CONTEXT_SCOPE,
        }:
            raise ValueError(f"unknown subagent context scope: {context_scope!r}")
        explicit_payload_only = context_scope == EXPLICIT_PAYLOAD_ONLY_CONTEXT_SCOPE
        preloaded: list[str] = []
        callable_skills: list[str] = []
        callable_native_tools: list[str] = []
        if not explicit_payload_only:
            # Keep the callable playbook surface declarative. A role's
            # ``allowed_skills`` is the boundary; the registry is only used
            # to discard stale ids. Native tools remain governed separately
            # by the parent tool policy below.
            preloaded = [
                skill
                for skill in (*spec.allowed_skills, *CHILD_CORE_SELF_CONTROL_SKILLS)
                if skill not in CHILD_SKILL_DENYLIST
            ]
            try:
                registry_ids = [
                    str(
                        getattr(getattr(entry, "manifest", None), "id", "")
                        or getattr(entry, "id", "")
                        or ""
                    )
                    for entry in self.skills.registry.list()
                ]
            except Exception:
                registry_ids = []
            declared_skills = set(preloaded)
            callable_skills = sorted({
                sid for sid in registry_ids
                if sid in declared_skills and sid not in CHILD_SKILL_DENYLIST
            })
            callable_native_tools = self._allowed_native_tool_names(
                spec=spec,
                delegation_depth=delegation_depth,
            )
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
        task_envelope, data_payload = _split_task_payload(payload)
        effective_subject_payload = {**task_envelope, **data_payload}

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
            "team_run_id": task_envelope.get("team_run_id"),
            "team_template": task_envelope.get("team_template"),
            "team_call_id": task_envelope.get("team_call_id") or parent_call_id,
            "team_task_id": task_envelope.get("task_id"),
            "team_task_owner": task_envelope.get("task_owner"),
            "team_task_subject": task_envelope.get("task_subject")
            or task_envelope.get("__team_task"),
        }
        safe_payload = redact_display_dict(data_payload)
        safe_task_envelope = redact_display_dict(task_envelope)

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

        base_context = (
            "" if explicit_payload_only else self._preloaded_skill_context(spec)
        )

        max_iter = self._max_iterations(spec)
        max_calls = 0 if explicit_payload_only else self._max_skill_calls(spec)
        configured_wall = self._max_wall_seconds(spec)
        if max_wall_seconds is not None:
            try:
                configured_wall = min(configured_wall, max(0.0, float(max_wall_seconds)))
            except (TypeError, ValueError):
                pass
        max_wall_seconds = configured_wall
        # A reserve must not consume the whole child turn before a repair
        # round starts. Preserve the configured value for normal budgets.
        finalization_reserve_seconds = self._finalization_reserve_seconds()
        if finalization_reserve_seconds >= max_wall_seconds:
            finalization_reserve_seconds = max(1.0, max_wall_seconds / 2)
        required_native_tools = [
            name for name in spec.execution_policy.required_native_tools
            if name in callable_native_tools
        ]
        required_tool_reminder_attempted = False
        consecutive_unproductive_batches = 0
        successful_call_signatures: set[str] = set()
        last_parsed: dict[str, Any] = {}
        last_raw: str = ""
        fatal_llm_error: str | None = None
        close_reason: str | None = None
        duplicate_recovery_attempted = False
        unstructured_protocol_retry_attempted = False
        total_tokens = 0
        total_usd = 0.0
        model_calls: list[dict[str, Any]] = []
        last_provider = ""
        last_model = ""
        audit_prompts: list[dict[str, Any]] = []
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
            "callable_skills": callable_skills,
            "native_tools": callable_native_tools,
            "context_chars": len(base_context or ""),
            "context_scope": context_scope,
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
            callable_skills=audit_start["callable_skills"],
            native_tools=audit_start["native_tools"],
            context_chars=audit_start["context_chars"],
        )

        def _dispatch_action(
            entry: Any,
            *,
            observation_iteration: int | str,
            event_iteration: int,
            signature: str,
        ) -> dict[str, Any] | None:
            dispatch_context_metadata = dict(team_event_fields)
            dispatch_context_metadata["remaining_wall_seconds"] = max(
                0.0,
                max_wall_seconds - (time.monotonic() - t_start),
            )
            record = self._dispatch_one(
                entry,
                spec_name=spec.name,
                allowed=callable_skills,
                allowed_native_tools=callable_native_tools,
                strategy_id=strategy_id,
                session_id=session_id,
                trigger_event_id=trigger_event_id,
                context_metadata=dispatch_context_metadata,
                execution_policy=spec.execution_policy,
                delegation_depth=delegation_depth,
                iteration=event_iteration,
                cancel_token=cancel_token,
            )
            if record is None:
                return None
            ok = bool(record.get("ok"))
            if ok:
                if signature:
                    successful_call_signatures.add(signature)
                skill_calls.append(record)
            else:
                rejected_actions.append(record)
            observation = {
                "iteration": observation_iteration,
                "skill": record.get("skill"),
                "action": record.get("action"),
                "ok": ok,
            }
            if ok:
                observation["summary"] = _summarise(record.get("result"))
            else:
                observation["error"] = record.get("error")
            event = {
                "step_kind": "act",
                "iteration": event_iteration,
                "status": "ok" if ok else "error",
                "skill": record.get("skill"),
                "action": record.get("action"),
            }
            if not ok:
                event["error"] = str(record.get("error") or "")
            _publish("subagent.step", **event)
            return observation

        accumulated_obs: list[dict[str, Any]] = []

        def _required_tool_gate(iteration: int) -> str:
            """Return ``retry``, ``close``, or ``""`` for policy requirements."""

            nonlocal required_tool_reminder_attempted, close_reason
            successful = {
                str(record.get("skill") or "")
                for record in skill_calls
                if record.get("ok")
            }
            missing = [
                name for name in required_native_tools
                if name not in successful
            ]
            if not missing:
                return ""
            has_retry_budget = (
                not required_tool_reminder_attempted
                and iteration + 1 < max_iter
                and max_wall_seconds - (time.monotonic() - t_start)
                > finalization_reserve_seconds
            )
            if has_retry_budget:
                required_tool_reminder_attempted = True
                accumulated_obs.append({
                    "iteration": iteration,
                    "ok": False,
                    "reason": "required_native_tool_missing",
                    "required_tools": missing,
                    "summary": (
                        "Execution policy requires one successful call to each "
                        "listed native tool before final output. Call the missing "
                        f"tool(s) now with valid arguments: {', '.join(missing)}."
                    ),
                })
                steps.append(_StepRecord(
                    kind="observe",
                    iteration=iteration,
                    status="retry",
                    detail={
                        "reason": "required_native_tool_missing",
                        "required_tools": missing,
                    },
                ))
                return "retry"
            close_reason = "required_native_tool_missing"
            return "close"

        def _mark_cancelled(iteration: int) -> bool:
            """Record one cooperative cancellation boundary."""

            nonlocal close_reason
            if not _token_is_set(cancel_token):
                return False
            reason = _token_reason(cancel_token)
            if (
                close_reason == reason
                and steps
                and steps[-1].kind == "close"
                and steps[-1].status == "cancelled"
            ):
                return True
            close_reason = reason
            steps.append(_StepRecord(
                kind="close",
                iteration=iteration,
                status="cancelled",
                error=reason,
            ))
            return True

        for i in range(max_iter):
            if _mark_cancelled(i):
                break
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
            remaining_wall_seconds = max_wall_seconds - (time.monotonic() - t_start)
            if accumulated_obs and remaining_wall_seconds <= finalization_reserve_seconds:
                close_reason = "subagent_finalization_reserve"
                break
            prompt = self._render_prompt(
                spec, data_payload, base_context, accumulated_obs,
                allowed=preloaded,
                native_tools=callable_native_tools,
                task_envelope=task_envelope,
                context_scope=context_scope,
            )
            audit_prompt = self._render_prompt(
                spec, safe_payload, base_context, accumulated_obs,
                allowed=preloaded,
                native_tools=callable_native_tools,
                task_envelope=safe_task_envelope,
                context_scope=context_scope,
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
            result = None
            llm_max_attempts = self._llm_max_attempts(spec)
            model_provider, model_id = self._model_override(spec)
            for llm_attempt in range(llm_max_attempts):
                try:
                    result = self.llm.call(
                        task="subagent_analysis", caller=f"subagent:{spec.name}",
                        tier=spec.tier, prompt=prompt,
                        model_provider=model_provider,
                        model_id=model_id,
                        metadata={
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "iteration": i,
                            "subagent": spec.name,
                            "strategy_id": strategy_id,
                            "trigger_event_id": trigger_event_id,
                            "parent_call_id": parent_call_id,
                            "context_scope": context_scope,
                            "team_run_id": task_envelope.get("team_run_id"),
                            "llm_attempt": llm_attempt + 1,
                        },
                    )
                    fatal_llm_error = None
                    break
                except Exception as exc:
                    err_msg = f"{type(exc).__name__}: {exc}"
                    can_retry = (
                        llm_attempt + 1 < llm_max_attempts
                        and _is_transient_subagent_llm_error(exc)
                        and time.monotonic() - t_start < max_wall_seconds
                    )
                    if can_retry:
                        steps.append(_StepRecord(
                            kind="think_retry",
                            iteration=i,
                            status="retry",
                            error=err_msg,
                            wall_ms=int((time.monotonic() - t0) * 1000),
                            detail={"llm_attempt": llm_attempt + 1},
                        ))
                        _publish(
                            "subagent.step",
                            step_kind="think_retry",
                            iteration=i,
                            status="retry",
                            error=err_msg,
                            wall_ms=int((time.monotonic() - t0) * 1000),
                            llm_attempt=llm_attempt + 1,
                        )
                        continue
                    fatal_llm_error = err_msg
                    steps.append(_StepRecord(
                        kind="think", iteration=i, status="error",
                        error=err_msg,
                        wall_ms=int((time.monotonic() - t0) * 1000),
                        detail={"llm_attempt": llm_attempt + 1},
                    ))
                    _publish(
                        "subagent.step",
                        step_kind="think", iteration=i, status="error",
                        error=err_msg,
                        wall_ms=int((time.monotonic() - t0) * 1000),
                        llm_attempt=llm_attempt + 1,
                    )
                    break
            if result is None:
                if (
                    fatal_llm_error
                    and close_reason is None
                    and (skill_calls or accumulated_obs or rejected_actions)
                ):
                    close_reason = "llm_error_after_tool_observations"
                break

            call_tokens = int(result.tokens or 0)
            call_usd = float(result.usd or 0.0)
            total_tokens += call_tokens
            total_usd += call_usd
            last_provider = str(getattr(result, "provider", "") or last_provider)
            last_model = str(getattr(result, "model", "") or last_model)
            model_calls.append({
                "iteration": i,
                "provider": last_provider,
                "model": last_model,
                "tier": spec.tier,
                "tokens": call_tokens,
                "usd": call_usd,
            })
            parsed = result.parsed if isinstance(result.parsed, dict) else {}
            last_parsed = parsed
            last_raw = result.raw
            think_wall = int((time.monotonic() - t0) * 1000)
            steps.append(_StepRecord(
                kind="think", iteration=i, status="ok",
                tokens=call_tokens,
                usd=call_usd,
                wall_ms=think_wall,
                detail={"keys": sorted(parsed.keys())[:8]},
            ))
            # Surface the model's chain-of-thought live. Mirrors what
            # ``nerya.agent.kernel`` publishes for the main agent's
            # think/replan steps so ``LiveActivity`` can render the
            # subagent's reasoning inline.
            reasoning_text = str(getattr(result, "reasoning_text", "") or "")
            _publish(
                "subagent.step",
                step_kind="think", iteration=i, status="ok",
                tokens=call_tokens,
                usd=call_usd,
                wall_ms=think_wall,
                reasoning=reasoning_text[:4000],
                reasoning_tokens=int(getattr(result, "reasoning_tokens", 0) or 0),
                reasoning_effort=str(
                    getattr(result, "reasoning_effort", "") or ""
                ),
                provider=str(getattr(result, "provider", "") or ""),
                model=str(getattr(result, "model", "") or ""),
                parsed_keys=sorted(parsed.keys())[:12],
            )

            # A cancellation can arrive while the provider call is in
            # flight. Preserve its usage telemetry, but do not execute the
            # actions it returned or start a follow-up/finalization call.
            if _mark_cancelled(i):
                break

            # Pick up contribution metadata the subagent produced.
            for sig in _coerce_list(parsed.get("signals") or parsed.get("signals_used")):
                if sig not in signals_used:
                    signals_used.append(str(sig))
            for ev in _coerce_list(parsed.get("evidence")):
                if isinstance(ev, dict):
                    evidence.append(ev)
                else:
                    evidence.append({"note": str(ev)})
            u = parsed.get("uncertainty")
            if u is not None:
                try:
                    uncertainty = max(0.0, min(1.0, float(u)))
                except (TypeError, ValueError):
                    pass

            # Dispatch any requested skill calls. Only allowed skills that are
            # not denylisted will run. Every attempt is recorded either as a
            # skill_call entry or as a rejected_actions entry.
            actions = _coerce_list(parsed.get("skill_calls"))
            settle_after_actions = (
                bool(actions)
                and parsed.get("replan") is False
                and parsed.get("continue") is not True
                and parsed.get("done") is not True
                and parsed.get("final") is not True
            )
            batch_obs: list[dict[str, Any]] = []
            batch_success = False
            duplicate_actions: list[dict[str, Any]] = []
            rejected_before_actions = len(rejected_actions)
            for entry in actions:
                if _mark_cancelled(i):
                    break
                if len(skill_calls) >= max_calls:
                    rejected_actions.append({
                        "entry": entry, "reason": "skill_call_budget_exhausted",
                    })
                    break
                signature = _skill_call_signature(entry)
                if signature and signature in successful_call_signatures:
                    skill, action = _skill_call_name_action(entry)
                    duplicate_actions.append(entry)
                    rejected_actions.append({
                        "entry": entry,
                        "skill": skill,
                        "action": action,
                        "reason": "duplicate_successful_skill_call",
                        "error": "duplicate_successful_skill_call",
                    })
                    continue
                observation = _dispatch_action(
                    entry,
                    observation_iteration=i,
                    event_iteration=i,
                    signature=signature,
                )
                if observation is not None:
                    batch_success = batch_success or observation["ok"] is True
                    batch_obs.append(observation)
            if any(
                str(record.get("error_kind") or "") == "permission_pending"
                for record in rejected_actions[rejected_before_actions:]
                if isinstance(record, dict)
            ):
                # Approval is an operator-owned pause, not an LLM-recoverable
                # tool failure. Continuing would spend another model call and
                # could produce a misleading done=true child output while the
                # outer handler still waits for approval.
                close_reason = "approval_pending"
                break
            if _token_is_set(cancel_token):
                # The action that was already in flight may have completed;
                # close before any settle/replan branch can request more work.
                _mark_cancelled(i)
                break
            if batch_obs:
                steps.append(_StepRecord(
                    kind="act", iteration=i, status="ok",
                    detail={
                        "observations_count": len(batch_obs),
                        "successful": batch_success,
                    },
                ))
                accumulated_obs.extend(batch_obs)
                if any(
                    str(observation.get("error_kind") or "")
                    == "permission_pending"
                    for observation in batch_obs
                ):
                    close_reason = "approval_pending"
                    break
            if actions and duplicate_actions and not batch_obs:
                duplicates = []
                for entry in duplicate_actions:
                    skill, action = _skill_call_name_action(entry)
                    duplicates.append({"skill": skill, "action": action})
                steps.append(_StepRecord(
                    kind="observe",
                    iteration=i,
                    status="ok",
                    detail={
                        "duplicate_successful_skill_calls": len(duplicate_actions),
                    },
                ))
                duplicate_observation = {
                    "iteration": i,
                    "ok": False,
                    "reason": "duplicate_successful_skill_call",
                    "summary": (
                        "duplicate successful tool request suppressed; produce "
                        "final analysis from the existing observations instead "
                        "of requesting the same data again"
                    ),
                    "duplicates": duplicates,
                }
                if not duplicate_recovery_attempted and i + 1 < max_iter:
                    duplicate_recovery_attempted = True
                    accumulated_obs.append(duplicate_observation)
                    continue
                close_reason = "duplicate_successful_tool_request"
                break
            if batch_obs and settle_after_actions:
                required_status = _required_tool_gate(i)
                if required_status == "retry":
                    continue
                if required_status == "close":
                    break
                close_reason = "tool_calls_without_replan_settled"
                steps.append(_StepRecord(
                    kind="observe",
                    iteration=i,
                    status="ok",
                    detail={
                        "settled_after_tool_calls": len(batch_obs),
                    },
                ))
                break

            if (
                not actions
                and not batch_obs
                and not skill_calls
                and not accumulated_obs
                and not unstructured_protocol_retry_attempted
                and i + 1 < max_iter
                and _is_unstructured_protocol_miss(parsed, result.raw)
            ):
                unstructured_protocol_retry_attempted = True
                accumulated_obs.append({
                    "iteration": i,
                    "ok": False,
                    "reason": "unstructured_output_protocol_retry",
                    "summary": (
                        "previous model output did not contain the required "
                        "JSON tool-call envelope or a final evidence-backed "
                        "analysis; continue with a valid skill_calls request "
                        "or return done=true with explicit evidence gaps"
                    ),
                })
                steps.append(_StepRecord(
                    kind="observe",
                    iteration=i,
                    status="retry",
                    detail={"reason": "unstructured_output_protocol_retry"},
                ))
                continue

            # Respect an explicit "done" signal; otherwise continue only if
            # the subagent explicitly asked for another pass. When the model
            # requested tool/skill calls we always give it one more turn with
            # those observations, even if it forgot to set replan=true.
            finishing_without_more_work = (
                parsed.get("done") is True
                or parsed.get("final") is True
                or (
                    not batch_obs
                    and not (parsed.get("continue") or parsed.get("replan"))
                )
            )
            if finishing_without_more_work:
                required_status = _required_tool_gate(i)
                if required_status == "retry":
                    continue
                if required_status == "close":
                    break
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
        iterations = sum(1 for step in steps if step.kind == "think")
        if close_reason is None and iterations >= max_iter:
            close_reason = "max_iterations"

        steps.append(_StepRecord(
            kind="close", iteration=len(steps),
            wall_ms=int((time.monotonic() - t_start) * 1000),
            detail={
                "iterations": iterations,
                "skill_calls": len(skill_calls),
                "rejected_actions": len(rejected_actions),
                "close_reason": close_reason,
            },
        ))

        def _try_final_observation_synthesis(reason: str) -> dict[str, Any] | None:
            nonlocal total_tokens, total_usd
            if not accumulated_obs:
                return None
            remaining = max_wall_seconds - (time.monotonic() - t_start)
            if remaining < 5.0:
                return None
            prompt = self._render_prompt(
                spec,
                data_payload,
                base_context,
                accumulated_obs,
                allowed=preloaded,
                native_tools=[],
                task_envelope=task_envelope,
                finalization_mode=True,
                context_scope=context_scope,
            )
            audit_prompt = self._render_prompt(
                spec,
                safe_payload,
                base_context,
                accumulated_obs,
                allowed=preloaded,
                native_tools=[],
                task_envelope=safe_task_envelope,
                finalization_mode=True,
                context_scope=context_scope,
            )
            safe_prompt = redact_text(audit_prompt)
            audit_prompts.append({
                "iteration": f"final:{reason}",
                "prompt": safe_prompt,
                "prompt_chars": len(audit_prompt),
                "redacted": True,
            })
            _publish(
                "subagent.step",
                step_kind="finalize",
                iteration=iterations,
                status="sent",
                prompt=safe_prompt,
                prompt_chars=len(audit_prompt),
                close_reason=reason,
            )
            t0 = time.monotonic()
            try:
                model_provider, model_id = self._model_override(spec)
                result = self.llm.call(
                    task="subagent_analysis",
                    caller=f"subagent:{spec.name}",
                    tier=spec.tier,
                    prompt=prompt,
                    model_provider=model_provider,
                    model_id=model_id,
                    metadata={
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "iteration": iterations,
                        "subagent": spec.name,
                        "strategy_id": strategy_id,
                        "trigger_event_id": trigger_event_id,
                        "parent_call_id": parent_call_id,
                        "context_scope": (
                            context_scope
                            if explicit_payload_only
                            else "subagent_finalization"
                        ),
                        "team_run_id": task_envelope.get("team_run_id"),
                        "finalization_reason": reason,
                    },
                )
            except Exception as exc:
                err_msg = redact_text(f"{type(exc).__name__}: {exc}")[:500]
                steps.append(_StepRecord(
                    kind="finalize",
                    iteration=iterations,
                    status="error",
                    error=err_msg,
                    wall_ms=int((time.monotonic() - t0) * 1000),
                    detail={"close_reason": reason},
                ))
                _publish(
                    "subagent.step",
                    step_kind="finalize",
                    iteration=iterations,
                    status="error",
                    error=err_msg,
                    wall_ms=int((time.monotonic() - t0) * 1000),
                    close_reason=reason,
                )
                return None
            parsed = result.parsed if isinstance(result.parsed, dict) else {}
            has_tool_requests = bool(parsed.get("skill_calls"))
            raw_text = str(result.raw or "")
            total_tokens_local = int(result.tokens or 0)
            total_usd_local = float(result.usd or 0.0)
            total_tokens += total_tokens_local
            total_usd += total_usd_local
            output = (
                None if has_tool_requests else _final_subagent_output(parsed, raw_text)
            )
            if output and not output.get("degraded"):
                output.setdefault("done", True)
            else:
                output = None
            steps.append(_StepRecord(
                kind="finalize",
                iteration=iterations,
                status="ok" if output is not None else "ignored",
                tokens=total_tokens_local,
                usd=total_usd_local,
                wall_ms=int((time.monotonic() - t0) * 1000),
                detail={
                    "close_reason": reason,
                    "accepted": output is not None,
                    "tool_request_rejected": has_tool_requests,
                    "keys": sorted(parsed.keys())[:8],
                },
            ))
            _publish(
                "subagent.step",
                step_kind="finalize",
                iteration=iterations,
                status="ok" if output is not None else "ignored",
                tokens=total_tokens_local,
                usd=total_usd_local,
                wall_ms=int((time.monotonic() - t0) * 1000),
                close_reason=reason,
                accepted=output is not None,
                tool_request_rejected=has_tool_requests,
                parsed_keys=sorted(parsed.keys())[:12],
            )
            return output

        cancelled = _token_is_set(cancel_token)
        final_output = _final_subagent_output(last_parsed, last_raw)
        if (
            not cancelled
            and close_reason == "tool_calls_without_replan_settled"
            and skill_calls
            and not (last_parsed.get("done") is True or last_parsed.get("final") is True)
        ):
            final_output = _try_final_observation_synthesis(close_reason) or _tool_observation_fallback_output(
                spec_name=spec.name,
                payload=effective_subject_payload,
                observations=accumulated_obs,
                skill_calls=skill_calls,
                rejected_actions=rejected_actions,
                close_reason=close_reason,
            )
        elif not cancelled and final_output.get("degraded") and skill_calls:
            final_output = _try_final_observation_synthesis(
                close_reason or str(final_output.get("error_kind") or "degraded_output")
            ) or _tool_observation_fallback_output(
                spec_name=spec.name,
                payload=effective_subject_payload,
                observations=accumulated_obs,
                skill_calls=skill_calls,
                rejected_actions=rejected_actions,
                close_reason=close_reason,
            )
        if fatal_llm_error and skill_calls:
            final_output["llm_error"] = redact_text(fatal_llm_error)[:500]
        final_output = _attach_data_coverage(
            final_output,
            requested_role=spec.name,
            role_profile=_spec_profile_name(spec),
            skill_calls=skill_calls,
            rejected_actions=rejected_actions,
        )
        if close_reason == "approval_pending":
            final_output.update({
                "done": False,
                "degraded": True,
                "error_kind": "approval_pending",
                "summary": "subagent is waiting for approval before continuing",
            })
        if cancelled:
            final_output.update({
                "done": True,
                "cancelled": True,
                "error_kind": "cancelled",
                "summary": f"subagent cancelled: {close_reason}",
            })
        if close_reason == "required_native_tool_missing":
            successful_tools = {
                str(record.get("skill") or "")
                for record in skill_calls
                if record.get("ok")
            }
            missing_tools = [
                name for name in required_native_tools
                if name not in successful_tools
            ]
            final_output["degraded"] = True
            final_output["error_kind"] = "required_native_tool_missing"
            final_output["summary"] = (
                "subagent did not satisfy its explicit execution contract; "
                f"missing successful tool calls: {', '.join(missing_tools)}"
            )
            final_output["required_tools_missing"] = missing_tools
        contribution_metrics = {
            "signals_used": signals_used,
            "skill_calls": skill_calls,
            "rejected_actions": rejected_actions,
            "uncertainty": uncertainty,
            "evidence": evidence,
        }
        _publish(
            "subagent.step",
            step_kind="close",
            iteration=len(steps),
            wall_ms=int((time.monotonic() - t_start) * 1000),
            iterations=iterations,
            skill_calls_n=len(skill_calls),
            rejected_actions_n=len(rejected_actions),
            tokens=total_tokens,
            usd=total_usd,
            error=fatal_llm_error,
            close_reason=close_reason,
        )
        _publish(
            "subagent.end",
            iterations=iterations,
            skill_calls=len(skill_calls),
            rejected=len(rejected_actions),
            tokens=total_tokens,
            usd=total_usd,
            error=fatal_llm_error,
            wall_ms=int((time.monotonic() - t_start) * 1000),
            output=redact_display_dict(final_output),
            metrics=redact_display_dict(contribution_metrics),
        )

        if fatal_llm_error and not last_parsed and not last_raw and not skill_calls:
            raise SubAgentLLMError(
                f"subagent {spec.name} LLM call failed before producing output: "
                f"{fatal_llm_error}"
            )

        return {
            "subagent": spec.name,
            "tier": spec.tier,
            "provider": last_provider,
            "model": last_model,
            "model_calls": model_calls,
            "output": final_output,
            "cancelled": cancelled,
            "close_reason": close_reason,
            "tokens": total_tokens,
            "usd": total_usd,
            "metrics": {**contribution_metrics, "iterations": iterations},
            "steps": [s.asdict() for s in steps],
            "audit": {
                **audit_start,
                "prompt_records": audit_prompts,
                "provider": last_provider,
                "model": last_model,
                "model_calls": model_calls,
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
        task_envelope: dict[str, Any] | None = None,
        finalization_mode: bool = False,
        context_scope: SubAgentContextScope = DEFAULT_CONTEXT_SCOPE,
        native_protocol: bool = False,
    ) -> str:
        task_envelope = task_envelope or {}
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
                f"name as the ``skill`` field): {preview}{tail}. Put all "
                "arguments in the ``payload`` object exactly as documented "
                "by the tool schema."
            )
            preferred_contracts: list[dict[str, Any]] = []
            preferred_native = set(allowed) & set(native_tools)
            for tool_name in allowed:
                if tool_name not in preferred_native or self.tool_registry is None:
                    continue
                descriptor = self.tool_registry.find(tool_name)
                if descriptor is None:
                    continue
                preferred_contracts.append({
                    "tool": tool_name,
                    "description": descriptor.description,
                    "payload_schema": descriptor.input_schema,
                })
            if preferred_contracts:
                nt_block += (
                    "\nPreferred native tool contracts for this role. The "
                    "payload must match these schemas exactly:\n"
                    + json.dumps(
                        preferred_contracts,
                        ensure_ascii=False,
                        default=str,
                    )
                )
        output_language = str(
            payload.get("output_language")
            or task_envelope.get("output_language")
            or payload.get("target_language")
            or task_envelope.get("target_language")
            or payload.get("response_language")
            or task_envelope.get("response_language")
            or ""
        ).strip()
        analysis_language = str(
            payload.get("analysis_language")
            or task_envelope.get("analysis_language")
            or payload.get("internal_language")
            or task_envelope.get("internal_language")
            or payload.get("working_language")
            or task_envelope.get("working_language")
            or payload.get("discussion_language")
            or task_envelope.get("discussion_language")
            or ""
        ).strip()
        language_block = ""
        if output_language and analysis_language and output_language != analysis_language:
            language_block = (
                "=== language contract ===\n"
                f"Role analysis language: {analysis_language}\n"
                f"Final report language: {output_language}\n"
                "Write natural-language JSON values, evidence summaries, "
                "rationales, and role conclusions in the analysis language. "
                "The parent team run will translate or synthesize the final "
                "user-facing report in the final report language. Preserve "
                "JSON keys, required enum values, proper nouns, tickers, "
                "source names, code identifiers, URLs, and numeric metrics.\n\n"
            )
        elif output_language:
            language_block = (
                "=== output language ===\n"
                f"Target user-visible language: {output_language}\n"
                "Write natural-language JSON values, evidence summaries, "
                "rationales, and role conclusions in this language. Preserve "
                "JSON keys, required enum values, proper nouns, tickers, "
                "source names, code identifiers, URLs, and numeric metrics.\n\n"
            )
        if finalization_mode:
            allow_note = (
                "\nFinalization mode: do not request any more tools. "
                "Produce the role's final evidence-backed analysis from the "
                "prior observations only. Return strict JSON with "
                '``"done": true`` and a concise ``summary``; include '
                "``evidence`` / ``risk_flags`` / ``uncertainty`` or explicit "
                "data gaps when useful. ``skill_calls`` are forbidden in "
                "this mode."
            )
        elif context_scope == EXPLICIT_PAYLOAD_ONLY_CONTEXT_SCOPE:
            allow_note = (
                "\nThis run is isolated to the explicit task payload. "
                "Do not request tools or rely on chat history, global memory, "
                "operator profile, or facts from another session. Produce the "
                "final analysis from the frozen payload only and include "
                '``"done": true``.'
            )
        elif native_protocol:
            allow_note = (
                "\nUse only the native tools provided by the caller through the "
                "tool API. Treat tool results as evidence for this run; do not "
                "print a skill_calls envelope in assistant text. "
                "When the role is complete, return one JSON object with "
                '``\"done\": true`` and a concise ``summary``. Preserve exact '
                "source URLs, paths, identifiers, and numeric values from tool "
                "results."
            )
        else:
            allow_note = (
                "\nYou may request skill calls via JSON "
                '``{"skill_calls": [{"skill": <id>, "action": <name>, '
                '"payload": {...}}]}``. '
                f"Preferred callable tools for this role: {allowed or 'none'}. "
                "Use only these declared ids and exact fields from the callable "
                "catalog; do not invent dotted actions or route names. "
                "A workspace skill describes a playbook; load it when needed "
                "and use the tool schemas for the actual call. "
                f"{nt_block}"
                '\nIf you are done, include ``"done": true``; to re-plan after '
                'these calls, include ``"replan": true``.'
            )
        if observations:
            allow_note += (
                "\nYou already have tool observations. Prefer producing the "
                'final role analysis now with ``"done": true``. Do not '
                "request the same data again; if a field is missing, state "
                "the evidence gap instead of looping on more tools. When the "
                "observations contain evidence-bearing source URLs or saved "
                "paths, cite at least one of those exact references in the "
                "role's final evidence/source fields instead of discarding "
                "the collected dataset."
            )
        assignment_block = _render_subagent_task_assignment(
            spec_name=spec.name,
            task_envelope=task_envelope,
        )
        assignment_section = (
            f"=== team assignment ===\n{assignment_block}\n\n"
            if assignment_block else ""
        )
        # Subagent prompts are otherwise date-free, so reasoning models
        # silently fall back to their training-cutoff worldview (observed:
        # 2026 team runs describing 2025 events as "upcoming" and citing
        # remembered TVL/ETF figures as current). One dated line plus an
        # explicit claim-grounding contract is the cheapest defense.
        grounding_section = (
            "=== session facts ===\n"
            f"Current datetime (UTC): {now_iso()}\n"
            "Grounding contract: any dated event or numeric market claim "
            "(prices, TVL, flows, upgrade/launch dates, filings) in your "
            "output must come from tool results or the provided context of "
            "THIS run. If you only remember it from training data, verify "
            "it with a tool first or label it explicitly as 'unverified, "
            "from memory, may be stale' — never present remembered figures "
            "or pre-cutoff timelines as current facts.\n\n"
        )
        observed_successes = {
            str(item.get("skill") or "")
            for item in observations
            if isinstance(item, dict) and item.get("ok") is True
        }
        required_remaining = [
            name for name in spec.execution_policy.required_native_tools
            if name in (native_tools or []) and name not in observed_successes
        ]
        execution_contract_section = ""
        if required_remaining and not finalization_mode:
            execution_contract_section = (
                "=== explicit execution contract ===\n"
                "Before returning final output, successfully call each required "
                "native tool exactly once: "
                f"{', '.join(required_remaining)}. This is a caller-supplied "
                "acceptance contract, not an inferred workflow.\n\n"
            )
        return (
            f"You are the {spec.name} subagent.\n"
            f"{spec.prompt or ''}\n\n"
            f"{grounding_section}"
            f"{execution_contract_section}"
            f"{assignment_section}"
            f"=== task payload ===\n"
            f"{wrap_untrusted('payload', json.dumps(payload, ensure_ascii=False, default=str))}\n\n"
            f"{language_block}"
            f"=== context ===\n{context}\n"
            f"{obs_block}{allow_note}\n"
        )

    # ---------------------------------------------------------------- dispatch
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
        if self.require_tool_executor and self.tool_executor is None:
            # Do not advertise a native surface that cannot pass the parent
            # policy chokepoint. _dispatch_native keeps a defense-in-depth
            # guard for stale/model-emitted names.
            return []
        policy = SubAgentExecutionPolicy.from_dict(
            getattr(spec, "execution_policy", None),
        )
        allow = set(policy.native_tool_allow)
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
            if allow and name not in allow:
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
        context_metadata: dict[str, Any] | None = None,
        execution_policy: SubAgentExecutionPolicy | None = None,
        delegation_depth: int = 0,
        iteration: int = 0,
        cancel_token: Any = None,
    ) -> dict[str, Any] | None:
        if _token_is_set(cancel_token):
            return {
                "ok": False,
                "skill": str(entry.get("skill") or "")
                if isinstance(entry, dict) else None,
                "action": str(entry.get("action") or "")
                if isinstance(entry, dict) else None,
                "error": _token_reason(cancel_token),
                "error_kind": "cancelled",
                "entry": entry,
            }
        if not isinstance(entry, dict):
            return {
                "ok": False, "skill": None, "action": None,
                "error": "skill_call entry is not a dict",
                "entry": entry,
            }
        skill = str(entry.get("skill") or "").strip()
        action = str(entry.get("action") or "").strip()
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
        # Native tools and playbook skills share one exact-name dispatch
        # surface. The payload is passed through unchanged and validated by
        # the native executor or skill action schema.
        native_names = allowed_native_tools or []
        if skill in native_names:
            native_payload = dict(payload or {})
            descriptor = (
                self.tool_registry.find(skill)
                if self.tool_registry is not None
                else None
            )
            schema_properties = (
                descriptor.input_schema.get("properties", {})
                if descriptor is not None
                and isinstance(descriptor.input_schema, dict)
                else {}
            )
            if (
                action
                and "action" in schema_properties
                and "action" not in native_payload
            ):
                native_payload["action"] = action
            return self._dispatch_native(
                skill, payload=native_payload, entry=entry,
                spec_name=spec_name,
                strategy_id=strategy_id, session_id=session_id,
                trigger_event_id=trigger_event_id,
                context_metadata=context_metadata,
                execution_policy=execution_policy,
                delegation_depth=delegation_depth,
                iteration=iteration,
                cancel_token=cancel_token,
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
        # ``allowed`` is the role's declared playbook surface. Unknown or
        # undeclared ids fail closed; native tools have already been handled
        # above against their registered schema.
        if skill not in allowed:
            return {
                "ok": False, "skill": skill, "action": action,
                "error": (
                    f"skill {skill!r} is not registered with the "
                    f"workspace skill runtime (or is denylisted for "
                    f"subagents)"
                ),
                "entry": entry,
            }
        if _token_is_set(cancel_token):
            return {
                "ok": False,
                "skill": skill,
                "action": action,
                "error": _token_reason(cancel_token),
                "error_kind": "cancelled",
                "entry": entry,
            }
        try:
            skill_kwargs: dict[str, Any] = {
                "payload": payload or {},
                "caller": f"subagent:{spec_name}",
                "strategy_id": strategy_id,
                "session_id": session_id,
                "trigger_event_id": trigger_event_id,
            }
            if cancel_token is not None:
                skill_kwargs["extras"] = {
                    "cancel_token": cancel_token,
                    "remaining_wall_seconds": (
                        (context_metadata or {}).get("remaining_wall_seconds")
                    ),
                }
            result = self.skills.runtime.call(skill, action, **skill_kwargs)
        except Exception as exc:
            failure: dict[str, Any] = {
                "ok": False, "skill": skill, "action": action,
                "error": f"{type(exc).__name__}: {exc}",
                "entry": entry,
            }
            # Structured recovery hint so the model fixes the call on the
            # very next step instead of probing action names blindly.
            if type(exc).__name__ == "SkillNotFoundError":
                failure["recovery_hint"] = (
                    "To read a skill's playbook use "
                    '{"skill": "skill_view", "payload": {"skill_id": '
                    f'"{skill}"}}}}. To execute, pick an action that the '
                    "skill actually declares (see the allowed tools list)."
                )
            return failure
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
        context_metadata: dict[str, Any] | None = None,
        execution_policy: SubAgentExecutionPolicy | None = None,
        delegation_depth: int = 0,
        iteration: int = 0,
        cancel_token: Any = None,
    ) -> dict[str, Any]:
        """Invoke a parent native tool from inside a subagent.

        The normal parent path invokes the shared
        :class:`NativeToolExecutor`, preserving schema validation,
        permission/approval decisions, risk classification, and hooks. A
        direct-handler fallback remains only for legacy callers that never
        supplied an executor (older isolated tests / ad-hoc runtimes).
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

        policy = SubAgentExecutionPolicy.from_dict(execution_policy)
        defaults = policy.tool_argument_defaults.get(tool_name) or {}
        payload = {**dict(defaults), **dict(payload or {})}
        safe_payload = redact_display_dict(dict(payload or {}))
        call = ToolCall(
            name=tool_name,
            arguments=dict(payload or {}),
            caller=f"subagent:{spec_name}",
            turn_id=str((context_metadata or {}).get("turn_id") or ""),
            iteration=max(0, int(iteration or 0)),
            metadata={
                **(context_metadata or {}),
                "subagent": spec_name,
                "strategy_id": strategy_id,
                "session_id": session_id,
                "trigger_event_id": trigger_event_id,
                "delegation_depth": max(0, int(delegation_depth or 0)),
                "cancel_token": cancel_token,
            },
        )
        try:
            if self.tool_executor is not None:
                raw = self.tool_executor.execute(call)
            elif self.require_tool_executor:
                return {
                    "ok": False,
                    "skill": tool_name,
                    "action": "(native)",
                    "tool_use_id": call.id,
                    "caller": call.caller,
                    "error": (
                        "native executor is required when a child tool registry "
                        "is inherited from the parent"
                    ),
                    "error_kind": "native_executor_required",
                    "payload": safe_payload,
                    "entry": entry,
                }
            else:
                # Compatibility for callers that construct a child runtime
                # directly without a parent kernel/executor.
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
                "tool_use_id": call.id, "caller": call.caller,
                "error": f"{type(exc).__name__}: {exc}",
                "payload": safe_payload,
                "entry": entry,
            }
        # Render the ToolResult into a dict the rest of the runtime
        # (``_summarise``, the journalled metrics block) can consume.
        result_dict = _tool_result_to_dict(raw)
        if result_dict.get("is_error"):
            error_detail = (
                result_dict.get("error_detail")
                if isinstance(result_dict.get("error_detail"), dict)
                else {}
            )
            return {
                "ok": False, "skill": tool_name, "action": "(native)",
                "tool_use_id": call.id,
                "caller": call.caller,
                "error": result_dict.get("error_message")
                or "native tool returned is_error=true",
                "error_kind": result_dict.get("error_kind"),
                "error_detail": error_detail,
                "recovery_hint": result_dict.get("recovery_hint") or {},
                "retryable": result_dict.get("retryable"),
                "result": result_dict,
                "payload": safe_payload,
                "entry": entry,
            }
        return {
            "ok": True, "skill": tool_name, "action": "(native)",
            "result": result_dict,
            "payload": safe_payload,
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
    error_kind = ""
    error_detail: dict[str, Any] = {}
    recovery_hint: dict[str, Any] = {}
    retryable: Any = None
    if isinstance(err, dict):
        error_message = str(err.get("message") or err.get("kind") or "")
        error_kind = str(err.get("kind") or "")
        detail = err.get("detail")
        if isinstance(detail, dict):
            error_detail = redact_display_dict(detail)
        hint = err.get("recovery_hint")
        if isinstance(hint, dict):
            recovery_hint = redact_display_dict(hint)
        retryable = err.get("retryable")
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
    if error_kind:
        out["error_kind"] = error_kind
    if error_detail:
        out["error_detail"] = error_detail
    if recovery_hint:
        out["recovery_hint"] = recovery_hint
    if retryable is not None:
        out["retryable"] = retryable
    return out


def _normalise_preloaded_tools(
    values: list[str],
    *,
    callable_skills: list[str],
    native_tools: list[str],
) -> list[str]:
    """Keep explicit role hints on the real callable surface."""

    callable_set = set(callable_skills) | set(native_tools)
    out: list[str] = []
    for raw in values:
        name = str(raw or "").strip()
        if not name:
            continue
        if name in callable_set and name not in out:
            out.append(name)
    return out


def _final_subagent_output(parsed: dict[str, Any], raw: str) -> dict[str, Any]:
    if isinstance(parsed, dict) and parsed:
        if _has_substantive_subagent_output(parsed):
            return parsed
        if parsed.get("skill_calls"):
            return {
                "raw": str(raw or ""),
                "degraded": True,
                "error_kind": "unfinished_tool_request",
                "summary": (
                    "subagent requested tool calls but did not produce a final analysis"
                ),
                "requested_tools": parsed.get("skill_calls"),
            }
        if set(parsed).issubset({"raw", "text", "message", "content"}):
            return {
                "raw": str(raw or ""),
                "degraded": True,
                "error_kind": "unstructured_output_without_evidence",
                "summary": (
                    "subagent produced unstructured text without a final "
                    "analysis or tool evidence"
                ),
            }
        return parsed
    text = str(raw or "")
    if text.strip():
        return {
            "raw": text,
            "degraded": True,
            "error_kind": "unstructured_output_without_evidence",
            "summary": (
                "subagent produced unstructured text without a final analysis "
                "or tool evidence"
            ),
        }
    return {
        "raw": "",
        "degraded": True,
        "error_kind": "empty_model_output",
        "summary": "subagent finished without visible final output",
    }


def _is_unstructured_protocol_miss(parsed: dict[str, Any], raw: str) -> bool:
    """Return true for child responses that did not produce a usable envelope.

    Raw/text wrappers are protocol misses when no parsed calls exist. The
    runtime can repair them once in-place using its bounded protocol retry.
    """

    if not isinstance(parsed, dict) or not parsed:
        return bool(str(raw or "").strip())
    if parsed.get("skill_calls"):
        return False
    if parsed.get("done") is True or parsed.get("final") is True:
        return False
    return set(parsed).issubset({"raw", "text", "message", "content"})


def _is_transient_subagent_llm_error(exc: BaseException) -> bool:
    if not isinstance(exc, LLMError):
        return False
    if isinstance(exc, _NON_RETRYABLE_SUBAGENT_LLM_ERRORS):
        return False
    msg = str(exc).lower()
    return any(hint.lower() in msg for hint in _TRANSIENT_SUBAGENT_LLM_HINTS)


def _has_substantive_subagent_output(parsed: dict[str, Any]) -> bool:
    """Return true when the model produced role analysis, not only tool calls."""

    analytical_keys = {
        "summary", "recommendation", "confidence", "thesis",
        "invalidation", "risk_flags", "evidence", "done", "final",
        "analysis", "findings", "conclusion", "verdict", "report",
        "status", "role", "data_inventory", "risk_policy",
        "parameter_table", "fundamentals", "valuation",
    }
    if set(parsed) & analytical_keys:
        return True
    protocol_keys = {
        "skill_calls", "continue", "replan", "raw", "text",
        "message", "content",
    }
    substantive_keys = [
        key for key, value in parsed.items()
        if key not in protocol_keys and value not in (None, "", [], {})
    ]
    return len(substantive_keys) >= 2


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
    requested_role: str = "",
    role_profile: str = "",
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
    financial_data_api = any(
        str(rec.get("skill") or "") == "data_api"
        and _data_api_financial_statement_observed(rec)
        for rec in skill_calls
        if rec.get("ok")
    )
    sec_filing_data = any(
        _sec_filing_observed(rec)
        for rec in skill_calls
        if rec.get("ok")
    )
    coverage = {
        "tools_used": tools_used,
        "tool_errors": tool_errors,
        "has_market_data": "market_data" in skills,
        "has_financial_statement": (
            "mcp__yahoo__get_financial_statement" in skills
            or financial_data_api
        ),
        "has_sec_filing": sec_filing_data,
        "has_stock_info": "mcp__yahoo__get_stock_info" in skills,
    }
    merged = dict(output)
    if requested_role:
        merged.setdefault("role", requested_role)
    if role_profile:
        merged.setdefault("role_profile", role_profile)
    merged["data_coverage"] = coverage
    contract = _evidence_contract_for_output(
        role_profile=role_profile,
        coverage=coverage,
    )
    if contract:
        merged["evidence_contract"] = contract
        missing = contract.get("missing_evidence") or []
        if missing:
            merged["missing_evidence"] = list(missing)
            merged.setdefault("partial", True)
            merged.setdefault("quality", "degraded_missing_evidence")
            merged.setdefault("error_kind", "insufficient_research_evidence")
    return merged


def _evidence_contract_for_output(
    *,
    role_profile: str,
    coverage: dict[str, Any],
) -> dict[str, Any]:
    if role_profile != "fundamentals_analyst":
        return {}
    required = {
        "market_snapshot": bool(
            coverage.get("has_market_data") or coverage.get("has_stock_info")
        ),
        "financial_statement": bool(coverage.get("has_financial_statement")),
    }
    missing = [name for name, ok in required.items() if not ok]
    return {
        "role_profile": role_profile,
        "status": "complete" if not missing else "degraded",
        "required_evidence": list(required),
        "missing_evidence": missing,
        "error_kind": "insufficient_research_evidence" if missing else None,
    }


def _data_api_financial_statement_observed(rec: dict[str, Any]) -> bool:
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    result = rec.get("result") if isinstance(rec.get("result"), dict) else {}
    provider = str(payload.get("provider") or result.get("provider") or "").lower()
    action = str(payload.get("action") or result.get("action") or "").lower()
    if provider not in {"financial_datasets", "equities", "financials", "sec_filings"}:
        return False
    return action in {
        "all_statements",
        "income_statements",
        "balance_sheets",
        "cash_flow_statements",
        "metrics_snapshot",
        "historical_metrics",
        "company_facts",
        "filings",
    }


def _sec_filing_observed(rec: dict[str, Any]) -> bool:
    skill = str(rec.get("skill") or "")
    if skill.startswith("mcp__edgar__"):
        return True
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    result = rec.get("result") if isinstance(rec.get("result"), dict) else {}
    provider = str(payload.get("provider") or result.get("provider") or "").lower()
    action = str(payload.get("action") or result.get("action") or "").lower()
    return (
        provider in {"financial_datasets", "equities", "financials", "sec_filings"} and action == "filings"
    )


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


def _skill_call_name_action(entry: Any) -> tuple[str, str]:
    if not isinstance(entry, dict):
        return "", ""
    skill = str(entry.get("skill") or "").strip()
    action = str(entry.get("action") or "").strip()
    return skill, action


def _skill_call_signature(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    skill, action = _skill_call_name_action(entry)
    if not skill:
        return ""
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    try:
        return json.dumps(
            {"skill": skill, "action": action, "payload": payload},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    except Exception:
        return f"{skill}\0{action}\0{payload!r}"


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
        text = (
            "{" + ", ".join(head) + (", _envelope: " + rest_text if rest else "") + "}"
        )
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            text = repr(result)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text
