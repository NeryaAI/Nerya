"""Provider-native loop contracts, independent of the execution engine.

Import these directly from callers that only need configuration or an outcome.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..llm.attempt_budget import DEFAULT_EXTRA_ATTEMPT_LIMIT
from .runtime import TurnSnapshot
from .transcript_blocks import BlockEnvelope

if TYPE_CHECKING:
    from .loop_state import TurnCheckpoint


@dataclass
class LoopConfig:
    """Caller-owned policy; the engine must not silently override these values.

    ``None`` disables optional budgets. A zero synthesis/action reserve disables
    that early-reserve policy, never the outer deadline or permission checks.
    Tool calls are bounded only by an explicit total. Retry allowances are shared
    across provider wire retries and logical recovery, including continuations.
    """

    turn_id: str | None = None
    max_iterations: int = 24
    compact_threshold: int = 60
    keep_tail_messages: int = 24
    max_tokens: int = 4096
    temperature: float = 0.2
    tier: str | None = None
    task: str = "agent.loop"
    caller: str = "agent:loop"
    reasoning_effort: str | None = None
    reasoning_summary: str | None = None
    model_provider: str | None = None
    model_id: str | None = None
    session_id: str | None = None
    strategy_id: str | None = None
    trigger_event_id: str | None = None
    required_artifacts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    max_wall_seconds: float | None = None
    wall_time_final_synthesis_seconds: float = 60.0
    # None keeps the conservative production reserve; zero is an explicit opt-out.
    action_tool_wall_reserve_seconds: float | None = None
    max_total_tool_calls: int | None = None
    repeated_tool_window: int = 5
    repeated_tool_threshold: int = 3
    repeated_tool_stop_after: int = 2
    max_extra_llm_attempts_per_turn: int = DEFAULT_EXTRA_ATTEMPT_LIMIT
    llm_retry_attempts: int = 10
    llm_retry_base_delay: float = 3.0
    llm_retry_max_delay: float = 60.0
    llm_retry_full_jitter: bool = True
    enable_microcompact: bool = True
    microcompact_max_chars: int = 8000
    microcompact_keep_recent: int = 3
    compact_preservation_cb: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None
    token_budget: int | None = None
    enable_diminishing_returns: bool = False
    diminishing_returns_threshold: int = 500
    diminishing_returns_window: int = 3
    reactive_compact_max_attempts: int = 3
    model_context_window: int | None = None
    token_pressure_compact_ratio: float = 0.85
    workspace_root: str | None = None
    tool_argument_defaults: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_call_metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "LoopConfig":
        """Read the one native configuration surface; caller identities are not config."""
        defaults = cls()
        casts = {
            "max_iterations": int, "compact_threshold": int, "keep_tail_messages": int,
            "max_tokens": int, "temperature": float, "wall_time_final_synthesis_seconds": float,
            "llm_retry_attempts": int, "max_extra_llm_attempts_per_turn": int,
            "llm_retry_base_delay": float, "llm_retry_max_delay": float, "llm_retry_full_jitter": bool,
            "enable_diminishing_returns": bool, "reactive_compact_max_attempts": int,
            "token_pressure_compact_ratio": float, "repeated_tool_window": int,
            "repeated_tool_threshold": int, "repeated_tool_stop_after": int,
            "enable_microcompact": bool, "microcompact_max_chars": int, "microcompact_keep_recent": int,
            "diminishing_returns_window": int, "diminishing_returns_threshold": int,
            "max_wall_seconds": float, "max_total_tool_calls": int, "token_budget": int,
            "model_context_window": int, "action_tool_wall_reserve_seconds": float,
        }
        tier = overrides.get("tier") or config.get("agent.native.tier")
        tier_config = config.get(f"llm.tiers.{tier}", {}) if tier else {}
        options = {"tier": tier}
        for name, cast in casts.items():
            fallback = getattr(defaults, name)
            if name in {"max_tokens", "temperature"}:
                fallback = (tier_config or {}).get(name, fallback)
            value = config.get(f"agent.native.{name}", fallback)
            if cast is bool and type(value) is not bool:
                raise ValueError(f"agent.native.{name} must be a boolean")
            options[name] = cast(value) if value is not None else None
        return cls(**{**options, **overrides})

    def model_options(self) -> dict[str, Any]:
        """One source of provider settings for every normal/recovery/final call."""
        return {
            "task": self.task,
            "caller": self.caller,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "tier": self.tier,
            "reasoning_effort": self.reasoning_effort,
            "reasoning_summary": self.reasoning_summary,
            "model_provider": self.model_provider,
            "model_id": self.model_id,
        }

    @property
    def tool_call_limit(self) -> int | None:
        return int(self.max_total_tool_calls) if self.max_total_tool_calls is not None else None


@dataclass
class LoopOutcome:
    """Public turn result. Token/cost totals are provider-reported, not estimates."""

    transcript: list[dict[str, Any]]
    iterations: int
    stop_reason: str
    final_text: str
    tool_calls: int
    error_count: int
    transition_reason: str = ""
    aborted: bool = False
    abort_reason: str = ""
    blocks: list[BlockEnvelope] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens_total: int = 0
    output_tokens_total: int = 0
    prompt_tokens_last: int = 0
    context_window: int = 0
    compaction_count: int = 0
    reactive_compaction_count: int = 0
    steer_messages: int = 0
    completion_status: str = "complete"
    completion_reason: str = ""
    completion_feedback: str = ""
    completion_rounds: int = 1
    provider: str = ""
    model: str = ""
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    usd_total: float = 0.0
    extra_llm_attempts: int = 0
    extra_llm_attempt_limit: int = 0
    extra_llm_attempts_by_reason: dict[str, int] = field(default_factory=dict)
    checkpoint: TurnCheckpoint | None = field(default=None, repr=False, compare=False)

    def snapshot(self, round_index: int, *, turn_id: str = "") -> TurnSnapshot:
        return TurnSnapshot(
            iteration=round_index,
            transcript=tuple(self.transcript),
            tool_results=tuple(
                block
                for message in self.transcript if isinstance(message, dict)
                for block in (message.get("content") if isinstance(message.get("content"), list) else [])
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ),
            output=self.final_text,
            stop_reason=self.stop_reason,
            usage={
                "llm_calls": self.llm_calls,
                "input_tokens": self.input_tokens_total,
                "output_tokens": self.output_tokens_total,
                "tool_calls": self.tool_calls,
            },
            metadata={
                "runtime": "root",
                "turn_id": self.checkpoint.turn_id if self.checkpoint else turn_id,
                "aborted": self.aborted,
                "abort_reason": self.abort_reason,
            },
        )
