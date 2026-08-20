"""Small, reusable state objects for the provider-native agent loop.

The main loop should orchestrate phases, not hand-maintain transport
partitions and a dozen telemetry counters. These helpers are deliberately
free of ``WorkspaceNativeAgentLoop`` imports so they can be tested and reused
by child runtimes without creating a second loop implementation.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any, Iterable, Mapping

from ..llm.attempt_budget import AttemptBudget, DEFAULT_EXTRA_ATTEMPT_LIMIT
from ..llm.messages import MessagesResponse
from ..llm.model_registry import lookup as _model_registry_lookup
from ..tools.types import (
    ContextModifier,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolResultPart,
)
from .transcript_blocks import BlockEnvelope


def provider_tool_name(tool: Any) -> str:
    """Return a provider-neutral tool name from Anthropic/OpenAI specs."""

    if not isinstance(tool, dict):
        return ""
    name = tool.get("name")
    if name:
        return str(name)
    function = tool.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function.get("name"))
    return ""


def filter_provider_tools_by_names(
    provider_tools: list[dict[str, Any]],
    tool_names: set[str] | tuple[str, ...],
) -> list[dict[str, Any]]:
    """Preserve provider order while selecting a declared tool subset."""

    names = {str(name) for name in tool_names if str(name)}
    if not names:
        return []
    return [
        tool
        for tool in provider_tools
        if provider_tool_name(tool) in names
    ]


@dataclass(frozen=True)
class ProviderToolSelection:
    """Partition provider-emitted tool calls against the advertised surface.

    ``calls`` preserves provider order for transcript pairing. ``offered`` and
    ``rejected`` make the policy decision explicit, so callers never need to
    rebuild the full list and then accidentally test the rebuilt list for
    emptiness (the dead branch that originally motivated this component).
    """

    calls: tuple[dict[str, Any], ...] = ()
    offered: tuple[dict[str, Any], ...] = ()
    rejected: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_blocks(
        cls,
        blocks: Iterable[dict[str, Any]],
        *,
        allowed_tool_names: Iterable[str],
    ) -> "ProviderToolSelection":
        allowed = frozenset(str(name) for name in allowed_tool_names if str(name))
        calls = tuple(
            block
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_use"
        )
        offered: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for call in calls:
            name = str(call.get("name") or "")
            if name and name in allowed:
                offered.append(call)
            else:
                rejected.append(call)
        return cls(
            calls=calls,
            offered=tuple(offered),
            rejected=tuple(rejected),
        )

    @property
    def only_rejected(self) -> bool:
        """Whether the provider emitted calls but none were advertised."""

        return bool(self.rejected) and not self.offered

    @property
    def rejected_names(self) -> tuple[str, ...]:
        return tuple(str(call.get("name") or "") for call in self.rejected)


@dataclass
class LoopUsage:
    """Provider-reported LLM usage and context-pressure state for one turn."""

    context_window: int = 0
    llm_calls: int = 0
    input_tokens_total: int = 0
    output_tokens_total: int = 0
    usd_total: float = 0.0
    provider: str = ""
    model: str = ""
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens_last: int = 0
    compaction_count: int = 0
    reactive_compaction_count: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens_total + self.output_tokens_total

    def record_response(
        self,
        response: MessagesResponse,
        *,
        iteration: int,
        context_scope: str = "agent_loop",
    ) -> None:
        """Record one completed model call, including text-only side paths.

        The previous loop updated counters only after its main provider call.
        Team-result synthesis and the wall-clock compact-final helper therefore
        disappeared from cost/model telemetry. Routing every successful
        ``MessagesResponse`` through this method keeps those paths observable.
        """

        raw_usage = response.usage if isinstance(response.usage, dict) else {}
        try:
            input_tokens = int(
                raw_usage.get("input_tokens")
                or raw_usage.get("prompt_tokens")
                or 0
            )
            output_tokens = int(
                raw_usage.get("output_tokens")
                or raw_usage.get("completion_tokens")
                or 0
            )
        except Exception:
            input_tokens = 0
            output_tokens = 0
        input_tokens = max(0, input_tokens)
        output_tokens = max(0, output_tokens)
        if input_tokens > 0 or output_tokens > 0:
            self.llm_calls += 1
            self.input_tokens_total += input_tokens
            self.output_tokens_total += output_tokens
            if input_tokens > 0:
                self.prompt_tokens_last = input_tokens

        provider = str(response.provider or "")
        model = str(response.model or "")
        if provider:
            self.provider = provider
        if model:
            self.model = model
        try:
            usd = max(0.0, float(getattr(response, "usd_cost", 0.0) or 0.0))
        except Exception:
            usd = 0.0
        self.usd_total += usd
        self.model_calls.append({
            "iteration": int(iteration),
            "context_scope": str(context_scope or "agent_loop"),
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "usd": usd,
        })

        if self.context_window <= 0 and (provider or model):
            try:
                self.context_window = int(
                    _model_registry_lookup(provider, model).context_window or 0
                )
            except Exception:
                self.context_window = 0

    def asdict(self) -> dict[str, Any]:
        return {
            "context_window": self.context_window,
            "llm_calls": self.llm_calls,
            "input_tokens_total": self.input_tokens_total,
            "output_tokens_total": self.output_tokens_total,
            "usd_total": self.usd_total,
            "provider": self.provider,
            "model": self.model,
            "model_calls": _clone_json(self.model_calls),
            "prompt_tokens_last": self.prompt_tokens_last,
            "compaction_count": self.compaction_count,
            "reactive_compaction_count": self.reactive_compaction_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "LoopUsage":
        data = dict(value or {})
        return cls(
            context_window=max(0, int(data.get("context_window") or 0)),
            llm_calls=max(0, int(data.get("llm_calls") or 0)),
            input_tokens_total=max(0, int(data.get("input_tokens_total") or 0)),
            output_tokens_total=max(0, int(data.get("output_tokens_total") or 0)),
            usd_total=max(0.0, float(data.get("usd_total") or 0.0)),
            provider=str(data.get("provider") or ""),
            model=str(data.get("model") or ""),
            model_calls=[
                dict(item)
                for item in (data.get("model_calls") or [])
                if isinstance(item, Mapping)
            ],
            prompt_tokens_last=max(0, int(data.get("prompt_tokens_last") or 0)),
            compaction_count=max(0, int(data.get("compaction_count") or 0)),
            reactive_compaction_count=max(
                0,
                int(data.get("reactive_compaction_count") or 0),
            ),
        )

    def outcome_kwargs(self) -> dict[str, Any]:
        """Return the stable ``LoopOutcome`` telemetry keyword shape."""

        return {
            "llm_calls": self.llm_calls,
            "input_tokens_total": self.input_tokens_total,
            "output_tokens_total": self.output_tokens_total,
            "prompt_tokens_last": self.prompt_tokens_last,
            "context_window": self.context_window,
            "compaction_count": self.compaction_count,
            "reactive_compaction_count": self.reactive_compaction_count,
            "provider": self.provider,
            "model": self.model,
            "model_calls": list(self.model_calls),
            "usd_total": self.usd_total,
        }


def _clone_json(value: Any) -> Any:
    """Return an isolated JSON-safe copy for durable checkpoint payloads."""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return deepcopy(value)


def _tool_result_from_dict(value: Mapping[str, Any]) -> ToolResult:
    error_value = value.get("error")
    error: ToolError | None = None
    if isinstance(error_value, Mapping):
        try:
            kind = ToolErrorKind(str(error_value.get("kind") or "unknown"))
        except ValueError:
            kind = ToolErrorKind.UNKNOWN
        error = ToolError(
            kind=kind,
            message=str(error_value.get("message") or ""),
            detail=dict(_clone_json(error_value.get("detail") or {})),
            retryable=error_value.get("retryable"),
            recovery_hint=dict(
                _clone_json(error_value.get("recovery_hint") or {})
            ),
        )
    content: list[ToolResultPart] = []
    for item in value.get("content") or []:
        if not isinstance(item, Mapping):
            continue
        content.append(
            ToolResultPart(
                type=str(item.get("type") or "text"),
                text=(
                    str(item.get("text"))
                    if item.get("text") is not None
                    else None
                ),
                data=_clone_json(item.get("data")),
                media_type=(
                    str(item.get("media_type"))
                    if item.get("media_type") is not None
                    else None
                ),
                metadata=dict(_clone_json(item.get("metadata") or {})),
            )
        )
    modifiers: list[ContextModifier] = []
    for item in value.get("context_modifiers") or []:
        if not isinstance(item, Mapping):
            continue
        modifiers.append(
            ContextModifier(
                kind=str(item.get("kind") or "custom"),
                path=(
                    str(item.get("path"))
                    if item.get("path") is not None
                    else None
                ),
                payload=dict(_clone_json(item.get("payload") or {})),
            )
        )
    semantic_success = value.get("semantic_success")
    return ToolResult(
        tool_use_id=str(value.get("tool_use_id") or ""),
        name=str(value.get("name") or ""),
        is_error=bool(value.get("is_error")),
        content=content,
        error=error,
        elapsed_ms=max(0, int(value.get("elapsed_ms") or 0)),
        completed_at=float(value.get("completed_at") or 0.0),
        context_modifiers=modifiers,
        metadata=dict(_clone_json(value.get("metadata") or {})),
        semantic_success=(
            bool(semantic_success) if semantic_success is not None else None
        ),
        result_protocol=str(value.get("result_protocol") or ""),
    )


def _block_envelope_from_dict(value: Mapping[str, Any]) -> BlockEnvelope:
    return BlockEnvelope(
        seq=max(0, int(value.get("seq") or 0)),
        turn_id=str(value.get("turn_id") or ""),
        message_id=str(value.get("message_id") or ""),
        role=str(value.get("role") or "assistant"),
        block=dict(_clone_json(value.get("block") or {})),
        ts=float(value.get("ts") or 0.0),
    )


TURN_CHECKPOINT_VERSION = 1


class TurnCheckpointResumeError(RuntimeError):
    """Structured fail-closed error for explicit durable continuation."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        status: int = 409,
    ) -> None:
        self.code = str(code or "turn_checkpoint_resume_error")
        self.status = int(status or 409)
        text = str(message or self.code)
        super().__init__(text)

    def asdict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.code,
            "message": str(self),
            "status": self.status,
        }


def validate_turn_checkpoint_resume_request(
    *,
    resume_turn_id: Any,
    continuation_feedback: Any,
    session_id: Any = None,
    turn_id: Any = None,
    has_attachments: bool = False,
) -> tuple[str, str]:
    """Normalize and validate the explicit durable continuation protocol."""

    resume_id = str(resume_turn_id or "").strip()
    feedback = str(continuation_feedback or "").strip()
    requested_turn_id = str(turn_id or "").strip()
    if bool(resume_id) != bool(feedback):
        raise TurnCheckpointResumeError(
            "turn_checkpoint_resume_fields_required",
            (
                "resume_turn_id and continuation_feedback must be provided "
                "together."
            ),
            status=400,
        )
    if not resume_id:
        return "", ""
    if not str(session_id or "").strip():
        raise TurnCheckpointResumeError(
            "turn_checkpoint_session_required",
            "Durable checkpoint continuation requires session_id.",
            status=400,
        )
    if requested_turn_id and requested_turn_id != resume_id:
        raise TurnCheckpointResumeError(
            "turn_checkpoint_turn_mismatch",
            (
                f"turn_id {requested_turn_id!r} does not match "
                f"resume_turn_id {resume_id!r}."
            ),
            status=409,
        )
    if has_attachments:
        raise TurnCheckpointResumeError(
            "turn_checkpoint_attachments_not_supported",
            (
                "Durable continuation cannot attach new files; start a new "
                "turn to introduce additional attachments."
            ),
            status=400,
        )
    return resume_id, feedback


@dataclass(frozen=True)
class TurnCheckpoint:
    """JSON-safe snapshot required to continue one provider-native turn.

    The checkpoint deliberately includes tool fingerprints, semantic-success
    state, budgets, one-shot retry flags, transcript blocks, and provider
    usage. A transcript-only checkpoint would allow a resumed turn to replay
    side effects or silently reset budgets.
    """

    version: int = TURN_CHECKPOINT_VERSION
    turn_id: str = ""
    message_id: str = ""
    seq: int = 0
    transcript: tuple[dict[str, Any], ...] = ()
    blocks: tuple[dict[str, Any], ...] = ()
    iterations: int = 0
    total_tool_calls: int = 0
    error_count: int = 0
    deadline_epoch: float | None = None
    tool_ledger: Mapping[str, Any] = field(default_factory=dict)
    control: Mapping[str, Any] = field(default_factory=dict)
    usage: Mapping[str, Any] = field(default_factory=dict)
    terminal: Mapping[str, Any] = field(default_factory=dict)
    resume_count: int = 0
    resumable: bool = True
    resume_block_reason: str = ""

    def __post_init__(self) -> None:
        version = int(self.version or 0)
        if version != TURN_CHECKPOINT_VERSION:
            raise ValueError(
                "unsupported turn checkpoint version: "
                f"{version}; expected {TURN_CHECKPOINT_VERSION}"
            )
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "turn_id", str(self.turn_id or ""))
        object.__setattr__(self, "message_id", str(self.message_id or ""))
        object.__setattr__(self, "seq", max(0, int(self.seq or 0)))
        object.__setattr__(
            self,
            "transcript",
            tuple(
                dict(item)
                for item in _clone_json(list(self.transcript or ()))
                if isinstance(item, dict)
            ),
        )
        object.__setattr__(
            self,
            "blocks",
            tuple(
                dict(item)
                for item in _clone_json(list(self.blocks or ()))
                if isinstance(item, dict)
            ),
        )
        object.__setattr__(self, "iterations", max(0, int(self.iterations or 0)))
        object.__setattr__(
            self,
            "total_tool_calls",
            max(0, int(self.total_tool_calls or 0)),
        )
        object.__setattr__(
            self,
            "error_count",
            max(0, int(self.error_count or 0)),
        )
        object.__setattr__(self, "tool_ledger", dict(_clone_json(self.tool_ledger)))
        object.__setattr__(self, "control", dict(_clone_json(self.control)))
        object.__setattr__(self, "usage", dict(_clone_json(self.usage)))
        object.__setattr__(self, "terminal", dict(_clone_json(self.terminal)))
        object.__setattr__(
            self,
            "resume_count",
            max(0, int(self.resume_count or 0)),
        )
        object.__setattr__(self, "resumable", bool(self.resumable))
        object.__setattr__(
            self,
            "resume_block_reason",
            str(self.resume_block_reason or ""),
        )

    def asdict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "turn_id": self.turn_id,
            "message_id": self.message_id,
            "seq": self.seq,
            "transcript": _clone_json(list(self.transcript)),
            "blocks": _clone_json(list(self.blocks)),
            "iterations": self.iterations,
            "total_tool_calls": self.total_tool_calls,
            "error_count": self.error_count,
            "deadline_epoch": self.deadline_epoch,
            "tool_ledger": _clone_json(self.tool_ledger),
            "control": _clone_json(self.control),
            "usage": _clone_json(self.usage),
            "terminal": _clone_json(self.terminal),
            "resume_count": self.resume_count,
            "resumable": self.resumable,
            "resume_block_reason": self.resume_block_reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TurnCheckpoint":
        return cls(
            version=int(
                value.get("version")
                if value.get("version") is not None
                else TURN_CHECKPOINT_VERSION
            ),
            turn_id=str(value.get("turn_id") or ""),
            message_id=str(value.get("message_id") or ""),
            seq=int(value.get("seq") or 0),
            transcript=tuple(value.get("transcript") or ()),
            blocks=tuple(value.get("blocks") or ()),
            iterations=int(value.get("iterations") or 0),
            total_tool_calls=int(value.get("total_tool_calls") or 0),
            error_count=int(value.get("error_count") or 0),
            deadline_epoch=(
                float(value.get("deadline_epoch"))
                if value.get("deadline_epoch") is not None
                else None
            ),
            tool_ledger=dict(value.get("tool_ledger") or {}),
            control=dict(value.get("control") or {}),
            usage=dict(value.get("usage") or {}),
            terminal=dict(value.get("terminal") or {}),
            resume_count=int(value.get("resume_count") or 0),
            resumable=bool(value.get("resumable", True)),
            resume_block_reason=str(value.get("resume_block_reason") or ""),
        )


@dataclass
class LoopRunState:
    """Mutable in-memory state whose projection is :class:`TurnCheckpoint`."""

    turn_id: str
    message_id: str
    deadline_epoch: float | None = None
    seq: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    blocks: list[BlockEnvelope] = field(default_factory=list)
    iterations: int = 0
    total_tool_calls: int = 0
    error_count: int = 0
    tool_result_by_fingerprint: dict[str, ToolResult] = field(default_factory=dict)
    completed_tool_results: list[ToolResult] = field(default_factory=list)
    recent_tool_fingerprints: list[str] = field(default_factory=list)
    deduped_counts_by_fingerprint: dict[str, int] = field(default_factory=dict)
    recovery_required_args_by_tool: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    attempted_tool_names: set[str] = field(default_factory=set)
    successful_tool_names: set[str] = field(default_factory=set)
    required_next_tool_names: set[str] = field(default_factory=set)
    next_action_nudges: set[tuple[str, ...]] = field(default_factory=set)
    required_artifact_announcements: set[tuple[str, ...]] = field(
        default_factory=set
    )
    interrupted_required_tool_retry_keys: set[tuple[str, ...]] = field(
        default_factory=set
    )
    transient_required_tool_retry_keys: set[
        tuple[tuple[str, ...], int, int]
    ] = field(default_factory=set)
    checkpointed_fingerprints: set[str] = field(default_factory=set)
    truncated_no_tool_retry_used: bool = False
    wall_time_final_synthesis_used: bool = False
    llm_safety_final_synthesis_retry_used: bool = False
    llm_safety_required_tool_retry_used: bool = False
    transient_final_synthesis_retry_used: bool = False
    text_only_final_attempt: bool = False
    preserved_pre_tool_answer: str = ""
    last_tool_batch_had_semantic_success: bool = False
    last_optional_tool_gap_notes: list[str] = field(default_factory=list)
    original_user_text: str = ""
    recent_text_lengths: list[int] = field(default_factory=list)
    diminishing_returns_triggered: bool = False
    usage: LoopUsage = field(default_factory=LoopUsage)
    attempt_budget: AttemptBudget = field(default_factory=AttemptBudget)
    steer_message_count: int = 0
    stop_reason: str = ""
    transition_reason: str = ""
    final_text: str = ""
    aborted_reason: str = ""
    resume_count: int = 0

    @classmethod
    def new(
        cls,
        *,
        turn_id: str,
        message_id: str,
        deadline_epoch: float | None,
        original_user_text: str,
        context_window: int,
        attempt_limit: int = DEFAULT_EXTRA_ATTEMPT_LIMIT,
    ) -> "LoopRunState":
        return cls(
            turn_id=turn_id,
            message_id=message_id,
            deadline_epoch=deadline_epoch,
            original_user_text=original_user_text,
            usage=LoopUsage(context_window=max(0, int(context_window or 0))),
            attempt_budget=AttemptBudget(limit=attempt_limit),
        )

    @classmethod
    def from_checkpoint(
        cls,
        checkpoint: TurnCheckpoint | Mapping[str, Any],
    ) -> "LoopRunState":
        cp = (
            checkpoint
            if isinstance(checkpoint, TurnCheckpoint)
            else TurnCheckpoint.from_dict(checkpoint)
        )
        ledger = dict(cp.tool_ledger or {})
        result_map = {
            str(key): _tool_result_from_dict(value)
            for key, value in (ledger.get("tool_result_by_fingerprint") or {}).items()
            if isinstance(value, Mapping)
        }
        completed_results = [
            _tool_result_from_dict(value)
            for value in ledger.get("completed_tool_results") or []
            if isinstance(value, Mapping)
        ]
        control = dict(cp.control or {})
        flags = dict(control.get("flags") or {})
        transient_retry_keys: set[tuple[tuple[str, ...], int, int]] = set()
        for item in control.get("transient_required_tool_retry_keys") or []:
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                continue
            names = tuple(str(name) for name in (item[0] or []))
            transient_retry_keys.add((names, int(item[1] or 0), int(item[2] or 0)))
        terminal = dict(cp.terminal or {})
        state = cls(
            turn_id=cp.turn_id,
            message_id=cp.message_id,
            deadline_epoch=cp.deadline_epoch,
            seq=cp.seq,
            transcript=[dict(item) for item in cp.transcript],
            blocks=[_block_envelope_from_dict(item) for item in cp.blocks],
            iterations=cp.iterations,
            total_tool_calls=cp.total_tool_calls,
            error_count=cp.error_count,
            tool_result_by_fingerprint=result_map,
            completed_tool_results=completed_results,
            recent_tool_fingerprints=[
                str(value)
                for value in ledger.get("recent_tool_fingerprints") or []
            ],
            deduped_counts_by_fingerprint={
                str(key): int(value or 0)
                for key, value in (
                    ledger.get("deduped_counts_by_fingerprint") or {}
                ).items()
            },
            recovery_required_args_by_tool={
                str(key): tuple(str(value) for value in values or [])
                for key, values in (
                    control.get("recovery_required_args_by_tool") or {}
                ).items()
            },
            attempted_tool_names={
                str(value) for value in ledger.get("attempted_tool_names") or []
            },
            successful_tool_names={
                str(value) for value in ledger.get("successful_tool_names") or []
            },
            required_next_tool_names={
                str(value) for value in ledger.get("required_next_tool_names") or []
            },
            next_action_nudges={
                tuple(str(value) for value in item or [])
                for item in control.get("next_action_nudges") or []
            },
            required_artifact_announcements={
                tuple(str(value) for value in item or [])
                for item in control.get("required_artifact_announcements") or []
            },
            interrupted_required_tool_retry_keys={
                tuple(str(value) for value in item or [])
                for item in control.get("interrupted_required_tool_retry_keys") or []
            },
            transient_required_tool_retry_keys=transient_retry_keys,
            checkpointed_fingerprints=set(result_map),
            truncated_no_tool_retry_used=bool(
                flags.get("truncated_no_tool_retry_used")
            ),
            wall_time_final_synthesis_used=bool(
                flags.get("wall_time_final_synthesis_used")
            ),
            llm_safety_final_synthesis_retry_used=bool(
                flags.get("llm_safety_final_synthesis_retry_used")
            ),
            llm_safety_required_tool_retry_used=bool(
                flags.get("llm_safety_required_tool_retry_used")
            ),
            transient_final_synthesis_retry_used=bool(
                flags.get("transient_final_synthesis_retry_used")
            ),
            text_only_final_attempt=False,
            preserved_pre_tool_answer=str(
                control.get("preserved_pre_tool_answer") or ""
            ),
            last_tool_batch_had_semantic_success=bool(
                control.get("last_tool_batch_had_semantic_success")
            ),
            last_optional_tool_gap_notes=[
                str(value)
                for value in control.get("last_optional_tool_gap_notes") or []
            ],
            original_user_text=str(control.get("original_user_text") or ""),
            recent_text_lengths=[
                max(0, int(value or 0))
                for value in control.get("recent_text_lengths") or []
            ],
            diminishing_returns_triggered=bool(
                control.get("diminishing_returns_triggered")
            ),
            usage=LoopUsage.from_dict(cp.usage),
            attempt_budget=AttemptBudget.from_dict(
                control.get("attempt_budget"),
            ),
            steer_message_count=max(
                0,
                int(control.get("steer_message_count") or 0),
            ),
            stop_reason=str(terminal.get("stop_reason") or ""),
            transition_reason=str(terminal.get("transition_reason") or ""),
            final_text=str(terminal.get("final_text") or ""),
            aborted_reason=str(terminal.get("aborted_reason") or ""),
            resume_count=cp.resume_count,
        )
        return state

    def prepare_continuation(self, feedback: str) -> str:
        """Reset terminal-only fields and append trusted gate feedback once."""

        self.checkpointed_fingerprints = set(self.tool_result_by_fingerprint)
        self.stop_reason = ""
        self.transition_reason = "completion_gate_continue"
        self.final_text = ""
        self.aborted_reason = ""
        self.text_only_final_attempt = False
        self.recent_text_lengths.clear()
        self.diminishing_returns_triggered = False
        self.last_tool_batch_had_semantic_success = False
        self.last_optional_tool_gap_notes.clear()
        self.resume_count += 1
        clean = str(feedback or "").strip()
        if not clean:
            clean = "Continue from the current turn state and address the completion gate."
        message = "[completion gate continuation]\n" + clean
        self.transcript.append(
            {"role": "user", "content": message, "pinned": True}
        )
        return message

    def to_checkpoint(
        self,
        *,
        resumable: bool,
        resume_block_reason: str = "",
    ) -> TurnCheckpoint:
        ledger = {
            "tool_result_by_fingerprint": {
                key: value.asdict()
                for key, value in self.tool_result_by_fingerprint.items()
            },
            "completed_tool_results": [
                value.asdict() for value in self.completed_tool_results
            ],
            "recent_tool_fingerprints": list(self.recent_tool_fingerprints),
            "deduped_counts_by_fingerprint": dict(
                self.deduped_counts_by_fingerprint
            ),
            "attempted_tool_names": sorted(self.attempted_tool_names),
            "successful_tool_names": sorted(self.successful_tool_names),
            "required_next_tool_names": sorted(self.required_next_tool_names),
        }
        control = {
            "recovery_required_args_by_tool": {
                key: list(values)
                for key, values in self.recovery_required_args_by_tool.items()
            },
            "next_action_nudges": [
                list(values) for values in sorted(self.next_action_nudges)
            ],
            "required_artifact_announcements": [
                list(values)
                for values in sorted(self.required_artifact_announcements)
            ],
            "interrupted_required_tool_retry_keys": [
                list(values)
                for values in sorted(self.interrupted_required_tool_retry_keys)
            ],
            "transient_required_tool_retry_keys": [
                [list(names), transcript_len, tool_calls]
                for names, transcript_len, tool_calls in sorted(
                    self.transient_required_tool_retry_keys
                )
            ],
            "flags": {
                "truncated_no_tool_retry_used": self.truncated_no_tool_retry_used,
                "wall_time_final_synthesis_used": self.wall_time_final_synthesis_used,
                "llm_safety_final_synthesis_retry_used": (
                    self.llm_safety_final_synthesis_retry_used
                ),
                "llm_safety_required_tool_retry_used": (
                    self.llm_safety_required_tool_retry_used
                ),
                "transient_final_synthesis_retry_used": (
                    self.transient_final_synthesis_retry_used
                ),
            },
            "preserved_pre_tool_answer": self.preserved_pre_tool_answer,
            "last_tool_batch_had_semantic_success": (
                self.last_tool_batch_had_semantic_success
            ),
            "last_optional_tool_gap_notes": list(
                self.last_optional_tool_gap_notes
            ),
            "original_user_text": self.original_user_text,
            "recent_text_lengths": list(self.recent_text_lengths),
            "diminishing_returns_triggered": self.diminishing_returns_triggered,
            "attempt_budget": self.attempt_budget.asdict(),
            "steer_message_count": self.steer_message_count,
        }
        return TurnCheckpoint(
            turn_id=self.turn_id,
            message_id=self.message_id,
            seq=self.seq,
            transcript=tuple(_clone_json(self.transcript)),
            blocks=tuple(block.as_dict() for block in self.blocks),
            iterations=self.iterations,
            total_tool_calls=self.total_tool_calls,
            error_count=self.error_count,
            deadline_epoch=self.deadline_epoch,
            tool_ledger=ledger,
            control=control,
            usage=self.usage.asdict(),
            terminal={
                "stop_reason": self.stop_reason,
                "transition_reason": self.transition_reason,
                "final_text": self.final_text,
                "aborted_reason": self.aborted_reason,
            },
            resume_count=self.resume_count,
            resumable=bool(resumable),
            resume_block_reason=str(resume_block_reason or ""),
        )


__all__ = [
    "LoopRunState",
    "LoopUsage",
    "TURN_CHECKPOINT_VERSION",
    "TurnCheckpoint",
    "TurnCheckpointResumeError",
    "validate_turn_checkpoint_resume_request",
    "ProviderToolSelection",
    "filter_provider_tools_by_names",
    "provider_tool_name",
]
