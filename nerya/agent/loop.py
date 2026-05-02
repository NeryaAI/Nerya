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

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..core.errors import (
    LLMApprovalRequired,
    LLMBudgetExceeded,
    LLMError,
    LLMScriptQuotaExceeded,
    LLMStructuredOutputError,
    LLMTaskNotAllowed,
    LLMTierDenied,
)
from ..harness.cancellation import CancelToken
from ..llm.gateway import LLMGateway
from ..llm.messages import MessagesResponse
from ..tools.orchestrator import ToolOrchestrator
from ..tools.registry import ToolRegistry
from ..tools.types import ToolCall, ToolResult
from .artifact_index import summarize_batch
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

    Apr-30 2026 — bumped 8 → 10 to match coding-agent's
    ``invokeWithRetries`` shape (coding-agent/agent.ts: ``maxRetries: 10``)
    so a sustained provider 5xx storm does not silently drop a turn."""

    llm_retry_base_delay: float = 3.0
    """Base seconds for exponential backoff between iteration-level
    LLM retries. Effective wait is ``base * 2^(attempt-1)`` capped at
    ``llm_retry_max_delay`` and then *full-jittered* (uniform(0, x)) —
    matching coding-agent's exponential-backoff-with-full-jitter strategy
    so a herd of concurrent agents doesn't synchronise their retries.
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
    of the bare exponential delay. Apr-30 2026 — runtime retry behavior:
    full jitter prevents thundering-herd retries when many agents share
    a provider account. Disable only for deterministic test runs."""

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
# nothing and just burns latency. Auth, tier policy, budget, schema, and
# explicit approval-required errors all fall in this bucket.
_NON_RETRYABLE_LLM_ERRORS: tuple[type[Exception], ...] = (
    LLMTierDenied,
    LLMBudgetExceeded,
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
            # count in budget; micro keeps every pair but truncates
            # bulky bodies (read/grep/glob/shell). Together they
            # mirror coding-agent's two-tier compaction.
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
                        # Apr-30 2026 — full jitter (coding-agent retry behavior):
                        # uniform(0, raw_delay). Avoids synchronised
                        # retries across concurrent agents sharing a
                        # provider account.
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
                    # Apr-30 2026 — instrument the retry so the operator
                    # can tell *why* a provider is 502'ing. The two
                    # main suspects are (a) upstream gateway flap
                    # (visible only as the bare HTTP code) and (b)
                    # request-too-large (visible as messages count +
                    # rough payload size). We surface both in the
                    # backend log and the frontend ThinkingBlock so a
                    # quick eyeball tells the difference: if every
                    # retry attempt sits at e.g. ~330k chars we're
                    # blowing the context, otherwise it's the gateway.
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
                    # Apr-30 2026 — pull the upstream body excerpt the
                    # provider attached to the LLMError (see
                    # ``nerya.llm.messages._make_llm_error``). On a 502
                    # this is usually a tiny HTML page from Cloudflare /
                    # nginx — the smoking-gun for "upstream gateway
                    # flap" vs "context-overflow".
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

            transcript.append({"role": "user", "content": tool_result_blocks})

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
        was_aborted = bool(aborted_reason) or (
            iterations >= self.config.max_iterations
            and stop_reason in {"tool_use", "tool_calls"}
        )
        if was_aborted and not aborted_reason:
            aborted_reason = "max_iterations"
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
        if tool_filter is not None:
            tools = [t for t in tools if tool_filter(t)]
        return [t.to_provider_tool() for t in tools]

    def _render_tool_result(self, result: ToolResult) -> dict[str, Any]:
        """Render a :class:`ToolResult` into an Anthropic ``tool_result`` block.

        On error we wrap the text in ``<tool_use_error>`` tags and
        append a one-line retry directive, mirroring Claude Code's
        ``toolExecution.ts:400`` / ``buildSchemaNotSentHint`` pattern.
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
        if not content:
            content.append({"type": "text", "text": result.text() or ""})

        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": result.tool_use_id,
            "content": content,
        }
        if not result.is_error:
            return block

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
