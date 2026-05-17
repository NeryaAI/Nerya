"""WorkspaceNativeAgentLoop — provider-native ``messages + tools`` loop.

This is the single canonical agent loop in Nerya. Each kernel turn
materialises a fresh :class:`WorkspaceNativeAgentLoop` and runs it
until the model emits ``stop_reason=end_turn`` (or a configurable
``max_iterations`` budget is exhausted).

Design summary (per

* The loop owns a *transcript* (list of provider-shaped messages).
* Each step calls :meth:`LLMGateway.call_messages` with the current
  transcript + tool registry.
* The model returns content blocks; we route ``tool_use`` blocks
  through :class:`ToolOrchestrator` (which gates them via the
  permission engine and dispatches via the executor).
* Tool results become a single follow-up ``user`` message containing
  one ``tool_result`` block per call (Anthropic shape — every other
  provider's blocks are translated to that shape inside
  :mod:`nerya.llm.messages`).
* Compaction is invoked whenever the transcript exceeds
  ``compact_threshold`` messages — pair invariants are preserved by
  :func:`compact_transcript`.

The loop is intentionally small. Anything not strictly part of "go
get the next assistant turn" lives elsewhere:

* Permission UI — ``executor.approval_cb``.
* Streaming events — emitted via the optional ``event_sink``.
* Persistence — the kernel saves the final transcript snapshot.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..core.errors import (
    LLMApprovalRequired,
    LLMError,
    LLMScriptQuotaExceeded,
    LLMStructuredOutputError,
    LLMTaskNotAllowed,
    LLMTierDenied,
)
from ..harness.cancellation import CancelToken
from ..llm.gateway import LLMGateway
from ..llm.messages import MessagesResponse
from ..llm import tool_compaction as _tool_compaction
from ..tools.orchestrator import ToolOrchestrator
from ..tools.registry import ToolRegistry
from ..tools.types import ToolCall, ToolResult
from .artifact_index import summarize_batch
from .attachments import assistant_attachment_block
from .transcript_blocks import (
    BlockEnvelope,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from .microcompact import microcompact
from .transcript_compact import compact_transcript


_LOG = logging.getLogger(__name__)


EventSink = Callable[[BlockEnvelope], None]


def _team_result_data(result: ToolResult) -> dict[str, Any] | None:
    if result.name != "team_run" or result.is_error:
        return None
    for part in result.content:
        if part.type == "json" and isinstance(part.data, dict):
            return part.data
    try:
        parsed = json.loads(result.text())
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


_REPORT_FIELD_ORDER = (
    "summary",
    "thesis",
    "recommendation",
    "verdict",
    "direction",
    "bias",
    "urgency",
    "quality",
    "growth",
    "valuation",
    "volatility_regime",
    "invalidation",
    "recommended_size_pct",
    "confidence",
    "avg_confidence",
)

_SKIP_REPORT_KEYS = {"done", "ok", "truncated"}


def _parse_jsonish(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text or text[0] not in "{[":
            return value
        try:
            parsed = json.loads(text)
        except Exception:
            return value
        return _parse_jsonish(parsed, depth=depth + 1)
    if isinstance(value, list):
        return [_parse_jsonish(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(k): _parse_jsonish(v, depth=depth + 1) for k, v in value.items()}
    return value


def _report_label(key: str) -> str:
    return key.replace("_", " ")


def _role_label(role: str) -> str:
    return role.replace("_", " ")


def _clip_report_text(text: str, *, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n_Content truncated for fallback rendering._"


def _format_scalar(value: Any, *, key: str = "") -> str:
    value = _parse_jsonish(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if key.endswith("_pct") and 0 <= float(value) <= 1:
            return f"{float(value) * 100:.1f}%"
        if isinstance(value, float):
            return f"{value:.4g}"
        return str(value)
    if value is None:
        return "n/a"
    return str(value).strip()


def _one_line(value: Any, *, key: str = "", limit: int = 700) -> str:
    value = _parse_jsonish(value)
    if isinstance(value, dict):
        parts: list[str] = []
        for child_key, child_value in value.items():
            if child_key in _SKIP_REPORT_KEYS:
                continue
            rendered = _one_line(child_value, key=child_key, limit=220)
            if rendered:
                parts.append(f"{_report_label(child_key)}: {rendered}")
            if len(parts) >= 6:
                break
        text = "; ".join(parts)
    elif isinstance(value, list):
        parts = [_one_line(item, limit=220) for item in value[:8]]
        text = "; ".join(part for part in parts if part)
        if len(value) > 8:
            text += f"; plus {len(value) - 8} more item(s)"
    else:
        text = _format_scalar(value, key=key)
    return text[:limit].rstrip() + ("..." if len(text) > limit else "")


def _record_primary(record: dict[str, Any]) -> tuple[str, str] | None:
    for key in (
        "claim",
        "event",
        "theme",
        "input",
        "risk",
        "name",
        "source",
        "symbol",
        "title",
    ):
        value = record.get(key)
        if value is None:
            continue
        text = _one_line(value, key=key, limit=260)
        if text:
            return key, text
    return None


def _format_record_bullet(record: dict[str, Any]) -> str:
    record = _parse_jsonish(record)
    if not isinstance(record, dict):
        return f"- {_one_line(record)}"
    primary = _record_primary(record)
    used: set[str] = set()
    if primary:
        primary_key, primary_text = primary
        used.add(primary_key)
        line = f"- **{primary_text}**"
    else:
        return "- " + _one_line(record, limit=900)

    details: list[str] = []
    detail_order = (
        "evidence",
        "reason",
        "severity",
        "confidence",
        "crowdedness",
        "purpose",
        "stop",
        "url",
    )
    ordered_keys = [
        key for key in detail_order if key in record and key not in used
    ] + [
        key
        for key in record
        if key not in used and key not in detail_order and key not in _SKIP_REPORT_KEYS
    ]
    for key in ordered_keys[:8]:
        value = record.get(key)
        rendered = _one_line(value, key=key, limit=420)
        if rendered:
            details.append(f"{_report_label(key)}: {rendered}")
    if details:
        line += ": " + "; ".join(details)
    return line


def _render_list_section(items: list[Any]) -> list[str]:
    lines: list[str] = []
    for item in items[:20]:
        item = _parse_jsonish(item)
        if isinstance(item, dict):
            lines.append(_format_record_bullet(item))
        else:
            lines.append(f"- {_one_line(item, limit=700)}")
    if len(items) > 20:
        lines.append(f"- {len(items) - 20} additional item(s) omitted.")
    return lines


def _render_dict_markdown(data: dict[str, Any]) -> str:
    data = _parse_jsonish(data)
    if not isinstance(data, dict):
        return _render_report_markdown(data)
    lines: list[str] = []
    keys = [
        key for key in _REPORT_FIELD_ORDER if key in data and key not in _SKIP_REPORT_KEYS
    ] + [
        key
        for key in data
        if key not in _REPORT_FIELD_ORDER and key not in _SKIP_REPORT_KEYS
    ]
    for key in keys:
        value = _parse_jsonish(data.get(key))
        if value in ("", None, [], {}):
            continue
        label = _report_label(key)
        if isinstance(value, list):
            lines.extend(["", f"#### {label}", *_render_list_section(value)])
        elif isinstance(value, dict):
            rendered = _one_line(value, key=key, limit=1200)
            if rendered:
                lines.append(f"- **{label}**: {rendered}")
        else:
            rendered = _format_scalar(value, key=key)
            if rendered:
                if key == "summary" and len(rendered) > 120:
                    lines.append(rendered)
                else:
                    lines.append(f"- **{label}**: {rendered}")
    return "\n".join(line for line in lines if line is not None).strip()


def _render_report_markdown(output: Any, *, limit: int = 4200) -> str:
    output = _parse_jsonish(output)
    if isinstance(output, dict):
        text = _render_dict_markdown(output)
    elif isinstance(output, list):
        text = "\n".join(_render_list_section(output))
    else:
        text = str(output or "").strip()
    return _clip_report_text(text, limit=limit)


def _synthesis_output(results: list[Any], aggregated: Any) -> Any:
    for preferred in ("research_manager", "lead_analyst", "portfolio_manager"):
        for entry in results:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("subagent") or "") == preferred:
                output = entry.get("output")
                if output not in (None, "", {}, []):
                    return output
    return aggregated


def _build_team_run_final_report(data: dict[str, Any]) -> str:
    task = str(data.get("task") or "AgentTeam analysis")
    status = str(data.get("status") or "")
    roles_succeeded = list(data.get("roles_succeeded") or [])
    roles_failed = list(data.get("roles_failed") or [])
    lines = [
        "# AgentTeam report",
        "",
        f"Task: {task}",
        f"Status: {status or 'completed'}",
        (
            "Team: "
            f"{len(roles_succeeded)} role(s) succeeded"
            + (f", {len(roles_failed)} role(s) failed/degraded" if roles_failed else "")
        ),
        "",
        "## Synthesis",
    ]
    results = data.get("results") if isinstance(data.get("results"), list) else []
    synthesis_text = _render_report_markdown(
        _synthesis_output(results, data.get("aggregated")),
        limit=3600,
    )
    if synthesis_text:
        lines.append(synthesis_text)
    if len(lines) <= 7:
        lines.append("The team returned role-level results; details follow by role.")

    if results:
        lines.extend(["", "## Role findings"])
        for entry in results:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("subagent") or "subagent")
            output = entry.get("output") or {}
            lines.extend([
                "",
                f"### {role} ({_role_label(role)})",
                _render_report_markdown(output),
            ])

    failures = data.get("failures") if isinstance(data.get("failures"), list) else []
    if failures:
        lines.extend(["", "## Gaps"])
        for entry in failures:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("subagent") or "subagent")
            err = str(entry.get("error") or entry.get("error_kind") or "unknown")
            lines.append(f"- {role}: {err[:600]}")

    lines.extend([
        "",
        "## Execution evidence",
        f"- team_run_id: {data.get('team_run_id')}",
        f"- roles_succeeded: {', '.join(map(str, roles_succeeded)) or 'none'}",
        f"- roles_failed: {', '.join(map(str, roles_failed)) or 'none'}",
        f"- tokens_total: {data.get('tokens_total', 0)}",
        f"- usd_total: {data.get('usd_total', 0.0)}",
    ])
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Loop config
# ---------------------------------------------------------------------------


@dataclass
class LoopConfig:
    max_iterations: int = 24
    """Hard ceiling on the number of model -> tools -> model rounds."""

    compact_threshold: int = 60
    """When transcript length exceeds this, run compaction."""

    keep_tail_messages: int = 24
    """How many recent messages to always preserve during compaction."""

    max_tokens: int = 4096
    temperature: float = 0.2
    tier: Optional[str] = None
    task: str = "agent.loop"
    caller: str = "agent:loop"
    reasoning_effort: Optional[str] = None
    reasoning_summary: Optional[str] = None
    model_provider: Optional[str] = None
    model_id: Optional[str] = None
    session_id: Optional[str] = None
    strategy_id: Optional[str] = None
    trigger_event_id: Optional[str] = None

    max_wall_seconds: Optional[float] = None
    """Wall-clock budget cap. ``None`` (default) means no cap — the
    loop only respects ``max_iterations``. When set, the loop checks
    elapsed time at the top of every iteration and aborts with
    ``stop_reason='timeout'`` once exceeded. Tool calls themselves
    have their own per-call timeouts (``run_shell.timeout_sec``,
    HTTP retries, …); this cap is the *outer* fence so a runaway
    agent can't burn through tokens or budget for hours.
    """

    max_total_tool_calls: Optional[int] = None
    """Optional per-turn total tool call budget. ``None`` defaults
    to ``max_iterations * 4`` — generous enough for normal turns
    but a fence against pathological loops where the model emits a
    big batch on every iteration."""

    llm_retry_attempts: int = 10
    """How many times to retry ``gateway.call_messages`` for one
    iteration when the provider returns a transient error (502 / 503
    / 504 / 500 / 429 / network timeout). The provider adapter
    *already* retries 5 times per HTTP call (see
    ``llm/adapters/_base._post_with_retry``); this layer is a second,
    longer fence that survives provider outages lasting tens of
    seconds — without it, a single bad iteration would drop a whole
    multi-minute turn whose tool history (reads/writes/etc.) is
    already on disk. Set to ``1`` to disable the loop-level retry.

    The default is high enough to ride out sustained provider 5xx bursts
    without silently dropping a long-running turn."""

    llm_retry_base_delay: float = 3.0
    """Base seconds for exponential backoff between iteration-level
    LLM retries. Effective wait is ``base * 2^(attempt-1)`` capped at
    ``llm_retry_max_delay`` and then *full-jittered* (uniform(0, x)) so a
    herd of concurrent agents does not synchronise its retries.
    With 10 attempts this gives a worst-case timeline of roughly
    3 + 6 + 12 + 24 + 48 + 60 + 60 + 60 + 60 = 333s (~5.5min), with
    the actual delays averaging ~half that under uniform jitter. Slow
    enough that a real provider outage almost always clears, fast
    enough that a transient blip on attempt 1 only adds a few
    seconds on average."""

    llm_retry_max_delay: float = 60.0
    """Hard cap (seconds) on each iteration-level retry sleep, before
    jitter is applied."""

    llm_retry_full_jitter: bool = True
    """If true, each retry sleeps ``uniform(0, computed_delay)`` instead
    of the bare exponential delay. Full jitter prevents thundering-herd
    retries when many agents share a provider account. Disable only for
    deterministic test runs."""

    enable_microcompact: bool = True
    """Run the per-tool-result token cap before every model round.
    Bulk read/grep/glob/shell results that exceed
    ``microcompact_max_chars`` get truncated to head + tail with a
    breadcrumb in the middle. Disable only for benchmarking compact
    behaviour; production should leave this on."""

    microcompact_max_chars: int = 8000
    microcompact_keep_recent: int = 3

    compact_preservation_cb: Optional[
        Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
    ] = None
    """Optional callback fired *after* macro-compaction, with the
    post-compact transcript. Returns the (possibly augmented)
    transcript. The kernel uses this to inject one synthetic
    system message listing files the agent had already read /
    edited (per :class:`FileStateCache`), so the model doesn't lose
    track of "these are the artefacts I'm working on" when the
    raw read/edit blocks were dropped during compaction. Idempotent
    — adding the same attachment twice should be a no-op."""


@dataclass
class LoopOutcome:
    """Final state after the loop completes (or aborts)."""

    transcript: list[dict[str, Any]]
    iterations: int
    stop_reason: str
    final_text: str
    tool_calls: int
    error_count: int
    aborted: bool = False
    abort_reason: str = ""
    blocks: list[BlockEnvelope] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Transient-error detection (loop-level retry on top of provider retries)
# ---------------------------------------------------------------------------


# These ``LLMError`` subclasses are *permanent* — retrying them buys
# nothing and just burns latency. Auth, tier policy, quota, schema, and
# explicit approval-required errors all fall in this bucket.
_NON_RETRYABLE_LLM_ERRORS: tuple[type[Exception], ...] = (
    LLMTierDenied,
    LLMTaskNotAllowed,
    LLMScriptQuotaExceeded,
    LLMStructuredOutputError,
    LLMApprovalRequired,
)


# Substrings that mark a generic ``LLMError`` as transient — the
# provider had a momentary blip we should sleep through. We match on
# the *message* (rather than just status codes) because the upstream
# adapter formats errors as ``"openai messages api error (502): http_502"``
# / ``"network timeout"`` / etc.
_TRANSIENT_LLM_HINTS: tuple[str, ...] = (
    "(429)",
    "(500)",
    "(502)",
    "(503)",
    "(504)",
    "(522)",
    "(524)",
    "rate_limit",
    "rate-limit",
    "rate limit",
    "timeout",
    "timed out",
    "connection",
    "network",
    "unreachable",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "ECONN",
    "ETIMEDOUT",
    "EAI_AGAIN",
)


def _is_transient_llm_error(exc: BaseException) -> bool:
    """Decide whether to retry the iteration after an LLM call fails.

    Returns ``False`` for any non-``LLMError`` (those propagate; the loop
    isn't responsible for catching foreign exceptions), for any of the
    known *permanent* ``LLMError`` subclasses, and for ``LLMError``
    messages that don't contain a transient-hint substring.
    """
    if not isinstance(exc, LLMError):
        return False
    if isinstance(exc, _NON_RETRYABLE_LLM_ERRORS):
        return False
    msg = str(exc).lower()
    for hint in _TRANSIENT_LLM_HINTS:
        if hint.lower() in msg:
            return True
    return False


def _build_deterministic_final_summary(
    *,
    stop_reason: str,
    abort_reason: str,
    iterations: int,
    tool_calls: int,
    error_count: int,
    had_model_text: bool,
) -> str:
    lines = [
        "Turn stopped before a complete model-written final answer was produced.",
        f"- stop_reason: {stop_reason or 'unknown'}",
        f"- abort_reason: {abort_reason or stop_reason or 'unknown'}",
        f"- iterations: {iterations}",
        f"- tool calls: {tool_calls}",
        f"- tool errors: {error_count}",
    ]
    if had_model_text:
        lines.append("- note: the model had emitted partial text, but not a reliable final answer.")
    else:
        lines.append("- note: no final assistant text was available after the last tool cycle.")
    lines.append(
        "Next: resume the same turn or retry with a narrower request so the agent can synthesize the completed tool results."
    )
    return "\n".join(lines)


def _stringify_user_message(message: str | list[dict[str, Any]]) -> str:
    if isinstance(message, str):
        return message.strip()
    parts: list[str] = []
    for item in message:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        if item.get("type") == "text":
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
            continue
        parts.append(json.dumps(item, ensure_ascii=False, default=str))
    return "\n".join(part for part in parts if part).strip()


def _clip_prompt_payload(text: str, *, limit: int = 50000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[truncated for final synthesis]"


def _build_team_run_final_synthesis_prompt(
    *,
    user_message: str | list[dict[str, Any]],
    team_results: list[dict[str, Any]],
) -> str:
    original_prompt = _stringify_user_message(user_message)
    conclusions = _clip_prompt_payload(
        json.dumps(team_results, ensure_ascii=False, indent=2, default=str)
    )
    return (
        "Produce the final answer for the completed AgentTeam run.\n\n"
        "Original user prompt:\n"
        f"{original_prompt or '[empty prompt]'}\n\n"
        "AgentTeam conclusions (all roles, failures, and aggregate data):\n"
        "```json\n"
        f"{conclusions}\n"
        "```\n\n"
        "Instructions:\n"
        "- Answer the original user prompt directly using the AgentTeam "
        "conclusions above.\n"
        "- Use the same natural language as the original user prompt for all "
        "user-visible prose. Infer it from the prompt itself; do not rely on "
        "fixed language-name mappings.\n"
        "- Synthesize and translate member outputs, headings, labels, and "
        "natural-language schema fields as needed so the final report is not "
        "mixed-language just because the tool data used another language.\n"
        "- Preserve tickers, proper nouns, source names, URLs, code identifiers, "
        "and numeric metrics in their original form.\n"
        "- Do not dump raw JSON or expose internal schema keys unless the user "
        "explicitly asked for raw tool data."
    )


def _messages_response_text(response: MessagesResponse) -> str:
    parts: list[str] = []
    for block in response.content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = str(block.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


class WorkspaceNativeAgentLoop:
    """Main loop: ``messages -> tools -> tool_result -> messages``."""

    def __init__(
        self,
        *,
        gateway: LLMGateway,
        registry: ToolRegistry,
        orchestrator: ToolOrchestrator,
        config: Optional[LoopConfig] = None,
        event_sink: Optional[EventSink] = None,
    ) -> None:
        self.gateway = gateway
        self.registry = registry
        self.orchestrator = orchestrator
        self.config = config or LoopConfig()
        self.event_sink = event_sink

    def _synthesize_team_run_final_answer(
        self,
        *,
        system: str,
        user_message: str | list[dict[str, Any]],
        team_results: list[dict[str, Any]],
    ) -> str:
        prompt = _build_team_run_final_synthesis_prompt(
            user_message=user_message,
            team_results=team_results,
        )
        response = self.gateway.call_messages(
            task=self.config.task,
            caller=self.config.caller,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            tier=self.config.tier,
            reasoning_effort=self.config.reasoning_effort,
            reasoning_summary=self.config.reasoning_summary,
            model_provider=self.config.model_provider,
            model_id=self.config.model_id,
        )
        return _messages_response_text(response)

    # ------------------------------------------------------------------ run

    def run(
        self,
        *,
        system: str,
        user_message: str | list[dict[str, Any]],
        prior_messages: Optional[list[dict[str, Any]]] = None,
        tool_filter: Optional[Callable[[Any], bool]] = None,
        cancel_token: Optional[CancelToken] = None,
    ) -> LoopOutcome:
        """Run a turn until the model emits ``end_turn`` or budget runs out.

        ``cancel_token`` is an optional cooperative cancellation flag
        (the harness exposes it via ``register_token``). The loop
        checks it at the top of each iteration so an operator
        ``signal_cancel(turn_id)`` lands cleanly between rounds —
        the in-flight gateway call (which is the long pole) cannot be
        cancelled, but no further iterations will start once the flag
        is set.
        """

        turn_id = uuid.uuid4().hex[:12]
        message_id = uuid.uuid4().hex[:12]
        seq = 0
        blocks: list[BlockEnvelope] = []
        deadline: Optional[float] = (
            (time.time() + float(self.config.max_wall_seconds))
            if self.config.max_wall_seconds and self.config.max_wall_seconds > 0
            else None
        )
        max_total_calls: Optional[int] = (
            int(self.config.max_total_tool_calls)
            if self.config.max_total_tool_calls
            else None
        )

        def emit(role: str, payload: dict[str, Any]) -> None:
            nonlocal seq
            seq += 1
            env = BlockEnvelope(
                seq=seq,
                turn_id=turn_id,
                message_id=message_id,
                role=role,
                block=payload,
            )
            blocks.append(env)
            if self.event_sink is not None:
                try:
                    self.event_sink(env)
                except Exception:
                    _LOG.exception("event_sink failed")

        transcript: list[dict[str, Any]] = []
        # Replay prior user/assistant exchanges from earlier turns of
        # the same chat session so the model has actual conversation
        # context. The kernel rebuilds these from the journal; we
        # preserve order and only accept the simple text shape.
        if prior_messages:
            for prior in prior_messages:
                if not isinstance(prior, dict):
                    continue
                role = prior.get("role")
                content = prior.get("content")
                if role not in ("user", "assistant"):
                    continue
                if isinstance(content, str) and content.strip():
                    transcript.append({"role": role, "content": content})
                elif isinstance(content, list) and content:
                    transcript.append({"role": role, "content": list(content)})
        if isinstance(user_message, str):
            transcript.append({"role": "user", "content": user_message})
        else:
            transcript.append({"role": "user", "content": list(user_message)})

        provider_tools = self._render_tools(tool_filter)

        iterations = 0
        total_tool_calls = 0
        error_count = 0
        stop_reason = ""
        final_text = ""
        aborted_reason = ""

        while iterations < self.config.max_iterations:
            iterations += 1
            # Cooperative cancel: lets HTTP/SDK callers stop a runaway
            # turn between iterations. We can't kill the in-flight
            # gateway call, but no further round-trip starts.
            if cancel_token is not None and cancel_token.is_set:
                aborted_reason = (
                    f"cancelled:{cancel_token.reason or 'operator_interrupt'}"
                )
                stop_reason = "cancelled"
                break
            if deadline is not None and time.time() >= deadline:
                aborted_reason = "timeout"
                stop_reason = "timeout"
                break
            if max_total_calls is not None and total_tool_calls >= max_total_calls:
                aborted_reason = "max_tool_calls"
                stop_reason = "max_tool_calls"
                break
            transcript = self._maybe_compact(transcript)
            # Microcompact runs *after* macro-compact so the per-result
            # token cap operates on the same set of messages the model
            # is about to see. The two are independent: macro drops
            # whole tool_use/tool_result pairs to keep the message
            # Count in budget; micro keeps every pair but truncates
            # bulky bodies (read/grep/glob/shell). Together they form a
            # two-tier compaction pass.
            if self.config.enable_microcompact:
                transcript, _mc_report = microcompact(
                    transcript,
                    max_chars_per_result=self.config.microcompact_max_chars,
                    keep_recent_results=self.config.microcompact_keep_recent,
                )
                if _mc_report.truncated:
                    _LOG.debug(
                        "microcompact: truncated %d result(s), %d byte(s) dropped",
                        _mc_report.truncated, _mc_report.bytes_dropped,
                    )

            # Iteration-level retry loop. The provider adapter
            # already retries 5 times per HTTP call, so we only land
            # here after a *sustained* upstream failure (10s+ outage,
            # repeated 502 burst, etc.). Without this fence the whole
            # multi-minute turn — and all the tool history already on
            # disk — gets thrown away because of one bad iteration.
            response: Optional[MessagesResponse] = None
            llm_attempt = 0
            llm_max = max(1, int(self.config.llm_retry_attempts))
            llm_base = max(0.0, float(self.config.llm_retry_base_delay))
            llm_cap = max(llm_base, float(self.config.llm_retry_max_delay))
            while True:
                llm_attempt += 1
                try:
                    response = self.gateway.call_messages(
                        task=self.config.task,
                        caller=self.config.caller,
                        system=system,
                        messages=transcript,
                        tools=provider_tools,
                        max_tokens=self.config.max_tokens,
                        temperature=self.config.temperature,
                        tier=self.config.tier,
                        reasoning_effort=self.config.reasoning_effort,
                        reasoning_summary=self.config.reasoning_summary,
                        model_provider=self.config.model_provider,
                        model_id=self.config.model_id,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 — bounded by guard below
                    if not _is_transient_llm_error(exc):
                        raise
                    if llm_attempt >= llm_max:
                        _LOG.warning(
                            "loop.llm_retry: giving up after %d attempt(s): %s",
                            llm_attempt, exc,
                        )
                        # One last visible block before we re-raise so
                        # the frontend's "Turn failed" card has the
                        # retry timeline directly above it. Without
                        # this the operator sees a bare 502 and has no
                        # idea we already burned 4 attempts on it.
                        emit(
                            "assistant",
                            ThinkingBlock(
                                text=(
                                    f"[loop.retry] giving up after "
                                    f"{llm_attempt} attempts.\n"
                                    f"final error: {exc}"
                                ),
                            ).as_dict(),
                        )
                        raise
                    raw_delay = min(
                        llm_cap,
                        llm_base * (2 ** (llm_attempt - 1)),
                    )
                    if bool(self.config.llm_retry_full_jitter):
                        # Full jitter = uniform(0, raw_delay). This avoids
                        # synchronised retries across concurrent agents
                        # sharing a provider account.
                        import random as _rnd
                        delay = _rnd.uniform(0.0, raw_delay)
                    else:
                        delay = raw_delay
                    if deadline is not None:
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            # Wall-clock budget already exhausted —
                            # the outer loop will trip the timeout
                            # guard on the next iteration. Re-raise
                            # so the kernel can log a clean failure.
                            raise
                        delay = min(delay, max(0.0, remaining - 0.1))
                    # Instrument retries so operators can distinguish
                    # provider-side gateway failures from oversized request
                    # payloads. Message count plus rough payload size makes
                    # it obvious whether the turn is blowing the context or
                    # the upstream is simply failing.
                    try:
                        _msg_count = len(transcript)
                        _payload_chars = sum(
                            len(json.dumps(m, ensure_ascii=False, default=str))
                            for m in transcript
                        )
                    except Exception:
                        _msg_count = -1
                        _payload_chars = -1
                    _request_id = ""
                    for _attr in ("request_id", "x_request_id", "trace_id"):
                        v = getattr(exc, _attr, None)
                        if v:
                            _request_id = str(v)
                            break
                    # Pull the provider's body excerpt from the LLMError.
                    # On a 502 this is often the only concrete clue about
                    # whether the failure came from a gateway page or from
                    # request-size pressure deeper in the stack.
                    _raw_body = ""
                    rb = getattr(exc, "raw_body", "") or ""
                    if rb:
                        _raw_body = str(rb)[:240]
                    _status_code = getattr(exc, "status_code", 0) or 0
                    _LOG.warning(
                        "loop.llm_retry: transient error on attempt %d/%d, "
                        "sleeping %.1fs (msgs=%d, payload~%d chars, "
                        "request_id=%s) %s",
                        llm_attempt, llm_max, delay,
                        _msg_count, _payload_chars,
                        _request_id or "-", exc,
                    )
                    # Surface the retry to the dashboard via a
                    # ``thinking`` block — the frontend's
                    # ``liveEventsToBlocks`` already renders thinking
                    # cards in the timeline. Marking it with a clear
                    # ``[loop.retry]`` prefix lets the operator see
                    # exactly which iteration tripped the upstream
                    # error and what backoff window we're sitting
                    # through. Without this, the only place the retry
                    # is visible is the backend stdout, which the
                    # operator usually can't tail.
                    _diag_lines = [
                        f"[loop.retry] transient LLM error on "
                        f"attempt {llm_attempt}/{llm_max}, "
                        f"backing off {delay:.1f}s before retry.",
                        f"reason: {exc}",
                    ]
                    if _request_id:
                        _diag_lines.append(f"request_id: {_request_id}")
                    if _status_code:
                        _diag_lines.append(f"status_code: {_status_code}")
                    if _raw_body:
                        _diag_lines.append(f"upstream_body: {_raw_body}")
                    if _msg_count >= 0:
                        _diag_lines.append(
                            f"transcript: {_msg_count} message(s), "
                            f"~{_payload_chars} chars (helps diagnose "
                            f"context-overflow vs upstream flap)"
                        )
                    emit(
                        "assistant",
                        ThinkingBlock(text="\n".join(_diag_lines)).as_dict(),
                    )
                    # Cooperative cancel during the sleep so a user-
                    # initiated abort doesn't have to wait the full
                    # backoff. We poll every 250ms.
                    waited = 0.0
                    while waited < delay:
                        if cancel_token is not None and cancel_token.is_set:
                            raise
                        step = min(0.25, delay - waited)
                        time.sleep(step)
                        waited += step
            assert response is not None  # for type-checkers
            stop_reason = response.stop_reason
            assistant_blocks = list(response.content)
            transcript.append({"role": "assistant", "content": assistant_blocks})

            for block in assistant_blocks:
                btype = block.get("type")
                if btype == "text":
                    tb = TextBlock(text=str(block.get("text") or ""))
                    emit("assistant", tb.as_dict())
                    final_text = tb.text
                elif btype == "thinking":
                    th = ThinkingBlock(
                        text=str(block.get("thinking") or block.get("text") or ""),
                        summary=str(block.get("summary") or ""),
                    )
                    emit("assistant", th.as_dict())
                elif btype == "tool_use":
                    tu = ToolUseBlock(
                        action=str(block.get("name") or ""),
                        skill_id="native",
                        payload=dict(block.get("input") or {}),
                        call_id=str(block.get("id") or ""),
                        started_at=time.time(),
                    )
                    emit("assistant", tu.as_dict())
                elif btype in {"attachment", "image", "document", "file", "video", "audio"}:
                    emit("assistant", assistant_attachment_block(dict(block)))

            tool_uses = [b for b in assistant_blocks if b.get("type") == "tool_use"]
            if not tool_uses:
                break

            # partial / interrupted tool_use repair. When the
            # provider stopped because of ``max_tokens`` (or any
            # non-tool finish reason) we cannot trust that the
            # ``input`` JSON is complete; the model was cut off mid-
            # stream. Skip the orchestrator and synthesise an
            # interrupted ``tool_result`` so transcript invariants
            # hold (every tool_use has a matching tool_result), then
            # break out of the loop. The next operator turn or
            # subsequent retry will see the interruption hint.
            if stop_reason in {"max_tokens", "length", "content_filter"}:
                interrupted_results: list[dict[str, Any]] = []
                for tu in tool_uses:
                    cid = str(tu.get("id") or "")
                    name = str(tu.get("name") or "")
                    interrupted_results.append({
                        "type": "tool_result",
                        "tool_use_id": cid,
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "[harness] tool_use interrupted: provider "
                                    f"stop_reason={stop_reason!r}. The arguments "
                                    "JSON may be truncated; do not trust them. "
                                    "On the next turn, retry with a shorter "
                                    "request or break it into smaller calls."
                                ),
                            }
                        ],
                        "is_error": True,
                    })
                    emit("tool", {
                        "kind": "tool_result",
                        "call_id": cid,
                        "name": name,
                        "ok": False,
                        "error_kind": "aborted",
                        "error": f"interrupted: stop_reason={stop_reason}",
                    })
                transcript.append({"role": "user", "content": interrupted_results})
                aborted_reason = aborted_reason or f"interrupted_{stop_reason}"
                break

            calls = [
                ToolCall(
                    name=str(tu.get("name") or ""),
                    arguments=dict(tu.get("input") or {}),
                    id=str(tu.get("id") or ""),
                    turn_id=turn_id,
                    iteration=iterations,
                    caller=self.config.caller,
                    metadata={
                        "session_id": self.config.session_id,
                        "strategy_id": self.config.strategy_id,
                        "trigger_event_id": self.config.trigger_event_id,
                        "original_user_prompt": _stringify_user_message(user_message),
                    },
                )
                for tu in tool_uses
            ]

            batch = self.orchestrator.run_batch(calls)
            total_tool_calls += len(calls)
            error_count += batch.error_count

            # per-batch summary so dashboards / TUI can show
            # one-liners ("3× read_file, 1× edit_file (+1 err)") without
            # walking the transcript. Emitted via the same event sink the
            # block envelopes use; missing sink is a no-op.
            try:
                batch_summary = summarize_batch(results=batch.results)
                batch_summary["auto_retries"] = int(getattr(batch, "auto_retries", 0))
                batch_summary["parallel_calls"] = int(batch.parallel_calls)
                batch_summary["serial_calls"] = int(batch.serial_calls)
                emit("system", {"kind": "tool_batch_summary", **batch_summary})
            except Exception:
                _LOG.debug("batch summary emit failed", exc_info=True)

            tool_result_blocks: list[dict[str, Any]] = []
            for r in batch.results:
                tool_result_blocks.append(self._render_tool_result(r))
                trb = ToolResultBlock(
                    call_id=r.tool_use_id,
                    skill_id="native",
                    action=r.name,
                    ok=not r.is_error,
                    result=r.text() if not r.is_error else None,
                    error=(r.error.message if r.error else None) if r.is_error else None,
                    error_kind=(r.error.kind.value if r.error else None) if r.is_error else None,
                    elapsed_ms=float(r.elapsed_ms),
                    completed_at=r.completed_at,
                )
                emit("tool", trb.as_dict())
                for part in r.content:
                    if part.type not in {"image", "document", "file", "attachment", "video", "audio"}:
                        continue
                    payload = part.data if isinstance(part.data, dict) else {}
                    emit(
                        "tool",
                        assistant_attachment_block(
                            {
                                "type": part.type,
                                "source": payload.get("source") or payload,
                                "name": payload.get("name")
                                or part.metadata.get("name")
                                or r.name
                                or "tool-attachment",
                                "mime_type": part.media_type
                                or payload.get("mime_type")
                                or payload.get("media_type"),
                                "text": part.text,
                                "source_kind": "tool",
                            }
                        ),
                    )

            transcript.append({"role": "user", "content": tool_result_blocks})

            team_results = [
                data
                for data in (
                    _team_result_data(r)
                    for r in batch.results
                )
                if data is not None
            ]
            if team_results:
                try:
                    final_text = self._synthesize_team_run_final_answer(
                        system=system,
                        user_message=user_message,
                        team_results=team_results,
                    )
                except Exception:
                    _LOG.warning(
                        "team_run final synthesis failed; using deterministic fallback",
                        exc_info=True,
                    )
                    final_text = ""
                if not final_text:
                    final_text = "\n\n".join(
                        _build_team_run_final_report(data)
                        for data in team_results
                    )
                transcript.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": final_text}],
                })
                emit("assistant", TextBlock(text=final_text).as_dict())
                stop_reason = "end_turn"
                break

            # If any call in this batch landed on a permission-pending
            # gate, stop the turn here. The dashboard now shows an
            # actionable approval card for each pending call, and the
            # model can't make progress until the operator decides;
            # letting the loop continue would just have the model pick
            # a different action and bury the card under fresh blocks.
            # The next turn (after the operator approves/rejects) picks
            # up from the persisted approval state.
            if any(
                bool(r.is_error)
                and r.error is not None
                and r.error.kind is not None
                and r.error.kind.value == "permission_pending"
                for r in batch.results
            ):
                stop_reason = "approval_pending"
                break

            # Once tool_uses were emitted AND tool_results fed back, always
            # give the model another round to consume them. Some OpenAI-compat
            # providers mislabel ``stop_reason`` as ``end_turn`` even when a
            # tool_use block was emitted (the finish_reason=="stop" branch in
            # the adapter); breaking here on that mislabel meant the model
            # never saw its own tool_result and the turn ended with just a
            # pre-tool preamble like "让我先检查一下…". The only stop_reasons
            # that should abort the loop at this point are the hard-fail ones
            # already handled above (max_tokens/length/content_filter).
            # Everything else — including end_turn — falls through so the
            # next iteration re-consults the model with the tool_result in
            # hand.

        # Aborted = forcibly stopped by a fence (cancel / timeout /
        # tool-call budget / max_iterations with the model still
        # asking for more tools). End-of-turn / explicit stop reasons
        # don't count as aborts.
        last_msg = transcript[-1] if transcript else {}
        last_content = last_msg.get("content") if isinstance(last_msg, dict) else None
        ended_after_tool_result = (
            isinstance(last_content, list)
            and any(
                isinstance(part, dict) and part.get("type") == "tool_result"
                for part in last_content
            )
        )
        was_aborted = bool(aborted_reason) or (
            iterations >= self.config.max_iterations
            and (stop_reason in {"tool_use", "tool_calls"} or ended_after_tool_result)
        )
        if was_aborted and not aborted_reason:
            aborted_reason = "max_iterations"
        if was_aborted:
            existing_text = final_text.strip()
            summary = _build_deterministic_final_summary(
                stop_reason=stop_reason or (
                    "max_iterations"
                    if iterations >= self.config.max_iterations
                    else "aborted"
                ),
                abort_reason=aborted_reason,
                iterations=iterations,
                tool_calls=total_tool_calls,
                error_count=error_count,
                had_model_text=bool(existing_text),
            )
            final_text = (
                f"{existing_text}\n\n{summary}"
                if len(existing_text) >= 32
                else summary
            )
            transcript.append({
                "role": "assistant",
                "content": [{"type": "text", "text": final_text}],
            })
            emit("assistant", TextBlock(text=summary).as_dict())
        return LoopOutcome(
            transcript=transcript,
            iterations=iterations,
            stop_reason=stop_reason or (
                "max_iterations"
                if iterations >= self.config.max_iterations
                else "end_turn"
            ),
            final_text=final_text,
            tool_calls=total_tool_calls,
            error_count=error_count,
            aborted=was_aborted,
            abort_reason=aborted_reason,
            blocks=blocks,
        )

    # -------------------------------------------------------------- helpers

    def _render_tools(
        self, tool_filter: Optional[Callable[[Any], bool]]
    ) -> list[dict[str, Any]]:
        tools = self.registry.list_tools()
        # If the registry has a LazyMcpState attached, hide every tool
        # whose ``lazy=True`` until its namespace is described in this
        # session or marked always-eager.
        #
        # The state is duck-typed so the loop has zero compile-time
        # dep on nerya.mcp.lazy.
        lazy_state = getattr(self.registry, "lazy_mcp_state", None)
        if lazy_state is not None:
            is_visible = getattr(lazy_state, "is_visible", None)
            if callable(is_visible):
                tools = [t for t in tools if is_visible(t)]
        if tool_filter is not None:
            tools = [t for t in tools if tool_filter(t)]
        return [t.to_provider_tool() for t in tools]

    def _render_tool_result(self, result: ToolResult) -> dict[str, Any]:
        """Render a :class:`ToolResult` into an Anthropic ``tool_result`` block.

        On error we wrap the text in ``<tool_use_error>`` tags and
        append a one-line retry directive so the model knows to retry
        after fixing the tool-call shape.
        The tag shape is familiar across the Anthropic training
        distribution, which helps non-Claude models decode the
        recovery intent too. The long schema dump that used to leak
        into this block is now kept on ``ToolError.detail`` for
        dashboards/telemetry only.
        """

        content: list[dict[str, Any]] = []
        for part in result.content:
            if part.type == "text" and part.text is not None:
                content.append({"type": "text", "text": part.text})
            elif part.type == "json" and part.data is not None:
                import json as _json

                content.append(
                    {
                        "type": "text",
                        "text": _json.dumps(
                            part.data, ensure_ascii=False, default=str
                        ),
                    }
                )
            elif part.type == "diff" and part.text is not None:
                content.append({"type": "text", "text": part.text})
            elif part.type == "shell" and part.data is not None:
                stdout = (part.data or {}).get("stdout") or ""
                stderr = (part.data or {}).get("stderr") or ""
                exit_code = (part.data or {}).get("exit_code")
                shell_text = (
                    f"[exit={exit_code}]\n"
                    + (f"## stdout\n{stdout}\n" if stdout else "")
                    + (f"\n## stderr\n{stderr}\n" if stderr else "")
                )
                content.append({"type": "text", "text": shell_text})
            elif part.type in {"image", "document", "file", "attachment", "video", "audio"}:
                payload = part.data if isinstance(part.data, dict) else {}
                content.append(
                    {
                        "type": part.type if part.type != "attachment" else "file",
                        "source": payload.get("source") or payload,
                        "name": (
                            payload.get("name")
                            or part.metadata.get("name")
                            or "tool-attachment"
                        ),
                        "mime_type": part.media_type
                        or payload.get("mime_type")
                        or payload.get("media_type"),
                        "text": part.text,
                    }
                )
        if not content:
            content.append({"type": "text", "text": result.text() or ""})

        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": result.tool_use_id,
            "content": content,
        }
        if not result.is_error:
            return self._maybe_compact_tool_block(block, result)

        # Replace the user-visible content with a ``<tool_use_error>``
        # wrapped string + retry directive. Keeps the raw telemetry on
        # ``result.error`` untouched.
        err = result.error
        raw = (err.message if err else None) or result.text() or "Unknown error"
        kind = err.kind.value if err and err.kind else "execution_error"
        retry_line = self._retry_directive_for(kind, result)
        wrapped = f"<tool_use_error>{kind}: {raw}</tool_use_error>"
        if retry_line:
            wrapped += f"\n{retry_line}"
        block["content"] = [{"type": "text", "text": wrapped}]
        block["is_error"] = True
        return block

    def _maybe_compact_tool_block(
        self,
        block: dict[str, Any],
        result: ToolResult,
    ) -> dict[str, Any]:
        """Apply tool-result compaction at the LLM-injection boundary.

        Runs once per tool result before the block is appended to the
        transcript. Honors the ``runtime.tool_result_compaction`` flag so
        operators can disable compaction without redeploying. Audit-
        critical fields (see
        :data:`nerya.llm.tool_compaction.AUDIT_FIELDS`) are always
        preserved in the kept dict so trade ids, error codes, and risk
        reasons survive the reduction.
        """

        try:
            from ..runtime import feature_flags as ff
            if not ff.is_enabled(None, "runtime.tool_result_compaction"):
                return block
        except Exception:  # pragma: no cover - defensive
            pass

        content = block.get("content") or []
        # Estimate the byte size that will reach the LLM by re-serializing
        # only the text payloads (image/file blobs are passed through;
        # their references stay intact).
        text_chars = 0
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_chars += len(str(part.get("text") or ""))
        if text_chars < _tool_compaction._DEFAULT_SIZE_THRESHOLD:
            return block

        # Prefer the structured payload from the original tool result when
        # available — the reducers know how to extract metrics, status
        # counts, etc. from the raw dict/list shape. Fall back to the
        # concatenated text representation.
        structured: Any = None
        for part in result.content:
            if part.type == "json" and part.data is not None:
                structured = part.data
                break
            if part.type == "shell" and part.data is not None:
                structured = part.data
                break
        if structured is None:
            structured = result.text() or ""

        # Durably persist the raw payload BEFORE we swap in the compacted
        # summary so the operator (and downstream skills / SDK callers)
        # can always recover the original output via ``raw_ref`` — even
        # after the LLM transcript is rewritten. The store is gated
        # silently: any persistence failure falls back to the legacy
        # ``call:<tool_use_id>`` shape and the loop continues, while the
        # durable raw-result path stays best-effort.
        try:
            from ..llm.tool_raw_store import write_default as _raw_write
            durable_ref = _raw_write(
                tool_use_id=result.tool_use_id or "",
                tool_name=result.name or "tool",
                payload=structured,
            )
        except Exception:  # pragma: no cover - defensive
            durable_ref = ""
        raw_ref = durable_ref or f"call:{result.tool_use_id}"

        compacted = _tool_compaction.compact_tool_result(
            result.name or "tool",
            structured,
            raw_ref=raw_ref,
        )
        if compacted.skipped:
            return block

        # Replace the text payloads with the compacted summary + kept
        # audit fields, but leave image/file/other binary parts intact.
        summary_text = compacted.summary
        if compacted.kept:
            try:
                summary_text += "\n[compacted_kept]\n" + json.dumps(
                    compacted.kept, ensure_ascii=False, default=str
                )
            except Exception:  # pragma: no cover - defensive
                summary_text += "\n[compacted_kept] " + repr(compacted.kept)
        new_content: list[dict[str, Any]] = [
            {"type": "text", "text": summary_text}
        ]
        # Preserve non-text parts (images, files) — only text/json was the
        # bloat we wanted to reduce.
        for part in content:
            if isinstance(part, dict) and part.get("type") not in ("text",):
                new_content.append(part)

        block = dict(block)
        block["content"] = new_content
        block["compaction"] = {
            "rule_id": compacted.rule_id,
            "original_bytes": compacted.original_bytes,
            "compacted_bytes": compacted.compacted_bytes,
            "raw_ref": compacted.raw_ref,
        }
        return block

    def _retry_directive_for(self, kind: str, result: ToolResult) -> str:
        """Return one actionable sentence to append after every error.

        The goal is to keep the model on the tool-use track. On a
        schema failure we tell it to re-call the same tool; on a
        transient failure we tell it to retry once; on unrecoverable
        failures we tell it to stop. Mirrors the spirit of Claude
        Code's ``buildSchemaNotSentHint`` — one explicit instruction,
        no schema dump.
        """

        tool = result.name or "this tool"
        if kind == "schema_validation":
            return (
                f"Fix the payload and call `{tool}` again with the "
                "corrected arguments. Do not switch to writing code "
                "in chat — the operator asked you to DO something, "
                "not to describe it."
            )
        if kind in {"timeout", "rate_limit", "provider_error"}:
            return (
                f"Transient error. Retry `{tool}` once; if it fails "
                "again, report the issue to the operator and stop."
            )
        if kind == "permission_denied":
            return (
                "This lane does not permit the tool. Pick a different "
                "tool or ask the operator to switch lanes."
            )
        if kind == "permission_pending":
            return (
                "Approval is owed by the operator. Either wait for "
                "the approval event or send a message explaining the "
                "request."
            )
        if kind == "deduped":
            return (
                "Use the prior result already in the transcript; do "
                "not re-issue this exact call."
            )
        if kind == "budget":
            return (
                "Per-turn budget exhausted. Wrap up with "
                "send_message instead of calling more tools."
            )
        if kind == "unknown_tool":
            return (
                "The tool name was not recognised. Call tool_search "
                "or re-read the available-tools header and pick a "
                "registered tool."
            )
        return ""

    def _maybe_compact(
        self, transcript: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if len(transcript) <= self.config.compact_threshold:
            return transcript
        if self.event_sink is not None:
            try:
                self.event_sink("system", {
                    "kind": "system",
                    "event_kind": "compact.start",
                    "before_count": len(transcript),
                })
            except Exception:
                pass
        compacted, report = compact_transcript(
            transcript,
            keep_tail_messages=self.config.keep_tail_messages,
            max_messages=self.config.compact_threshold,
        )
        _LOG.info(
            "transcript compacted: kept=%d dropped=%d pairs_dropped=%d "
            "skills_preserved=%s",
            report.kept, report.dropped, report.pairs_dropped,
            report.skills_preserved,
        )
        # give the kernel a chance to re-attach file-state
        # / plan / async-task summaries that lived in the dropped
        # tool_use/tool_result pairs. The callback is responsible for
        # idempotency; we just hand it the compacted transcript and
        # accept whatever it returns.
        if self.config.compact_preservation_cb is not None:
            try:
                compacted = self.config.compact_preservation_cb(compacted)
            except Exception:
                _LOG.exception("compact_preservation_cb failed")
        if self.event_sink is not None:
            try:
                self.event_sink("system", {
                    "kind": "system",
                    "event_kind": "compact.complete",
                    "kept": int(report.kept),
                    "dropped": int(report.dropped),
                    "pairs_dropped": int(report.pairs_dropped),
                    "skills_preserved": list(report.skills_preserved or []),
                    "after_count": len(compacted),
                })
            except Exception:
                pass
        return compacted


__all__ = [
    "EventSink",
    "LoopConfig",
    "LoopOutcome",
    "WorkspaceNativeAgentLoop",
]
