"""ToolOrchestrator — batch-aware fan-out / fan-in over the executor.

A turn often produces multiple ``tool_use`` blocks at once (Claude
Code, IDE, and the Anthropic SDK all encourage this). The
orchestrator implements the same fan-out semantics:

* **Read-only concurrent** — descriptors flagged
  ``is_concurrency_safe=True`` execute in parallel via a thread pool.
* **Mutating serial**       — anything else runs one-at-a-time, in
  the order the model emitted, so context modifiers compose
  deterministically.
* **Mixed batch**           — the orchestrator splits the batch:
  read-only group first (parallel), then mutating group (serial),
  which keeps conflicting writes out of the parallel lane.
* **Context modifier replay** — modifiers are applied *after* the
  whole batch resolves, in tool-call order. This keeps the post-state
  consistent regardless of the parallel scheduling decisions made by
  the executor.

The orchestrator does **not** know about the LLM, the gateway, or
the transcript — it is a pure batch dispatcher over an executor.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from ..agent.error_recovery import RecoveryAction, classify_for_recovery, policy_for_kind
from .executor import NativeToolExecutor
from .registry import ToolRegistry
from ..harness.cancellation import CancelToken
from .execution_contracts import (
    dispatch_stop_reason, execution_unknown_result, is_read_only_tool,
    pair_executed_results, skipped_before_dispatch,
)
from .types import ContextModifier, ToolCall, ToolResult


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Modifier sink
# ---------------------------------------------------------------------------


ContextModifierSink = Callable[[ToolCall, ContextModifier], None]


# ---------------------------------------------------------------------------
# Result aggregator
# ---------------------------------------------------------------------------


@dataclass
class BatchResult:
    """Aggregated outcome of one fan-out batch."""

    results: list[ToolResult] = field(default_factory=list)
    total_elapsed_ms: int = 0
    parallel_calls: int = 0
    serial_calls: int = 0
    error_count: int = 0
    auto_retries: int = 0

    def by_tool_use_id(self) -> dict[str, ToolResult]:
        return {r.tool_use_id: r for r in self.results}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ToolOrchestrator:
    """Fan-out / fan-in driver over a :class:`NativeToolExecutor`."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        executor: NativeToolExecutor,
        max_parallel: int = 4,
        modifier_sink: Optional[ContextModifierSink] = None,
        auto_retry_transient: bool = True,
    ) -> None:
        self.registry = registry
        self.executor = executor
        self.max_parallel = max(1, int(max_parallel))
        self.modifier_sink = modifier_sink
        self.auto_retry_transient = bool(auto_retry_transient)

    def run_batch(self, calls: Iterable[ToolCall]) -> BatchResult:
        """Execute ``calls`` honouring concurrency-safety semantics."""

        ordered: list[ToolCall] = list(calls)
        if not ordered:
            return BatchResult()

        ro_indices: list[int] = []
        mutating_indices: list[int] = []
        for i, c in enumerate(ordered):
            if self._is_read_only(c):
                ro_indices.append(i)
            else:
                mutating_indices.append(i)

        results: list[Optional[ToolResult]] = [None] * len(ordered)
        auto_retry_total = 0

        # ----- read-only batch (parallel) ---------------------------------
        if ro_indices:
            workers = min(self.max_parallel, len(ro_indices))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tool-ro") as pool:
                fut_to_idx = {
                    pool.submit(self._execute_with_retry, ordered[i]): i for i in ro_indices
                }
                for fut in as_completed(fut_to_idx):
                    idx = fut_to_idx[fut]
                    try:
                        results[idx], retries_consumed = fut.result()
                        auto_retry_total += retries_consumed
                    except Exception:
                        _LOG.exception("RO tool crashed in batch: %s", ordered[idx].name)
                        results[idx] = execution_unknown_result(ordered[idx], "parallel dispatch failed")

        # ----- mutating batch (serial, original order) --------------------
        for idx in mutating_indices:
            try:
                results[idx], retries_consumed = self._execute_with_retry(ordered[idx])
                auto_retry_total += retries_consumed
            except Exception:
                _LOG.exception("mutating tool crashed: %s", ordered[idx].name)
                results[idx] = execution_unknown_result(ordered[idx], "serial dispatch failed")

        # ----- aggregate + replay modifiers in input order ----------------
        finalised: list[ToolResult] = []
        total_elapsed = 0
        errors = 0
        for i, c in enumerate(ordered):
            r = results[i]
            assert r is not None
            finalised.append(r)
            total_elapsed += r.elapsed_ms
            if r.is_error:
                errors += 1
            if self.modifier_sink is not None:
                for mod in r.context_modifiers:
                    try:
                        self.modifier_sink(c, mod)
                    except Exception:
                        _LOG.exception("context modifier sink failed for %s", c.name)

        return BatchResult(
            results=finalised,
            total_elapsed_ms=total_elapsed,
            parallel_calls=len(ro_indices),
            serial_calls=len(mutating_indices),
            error_count=errors,
            auto_retries=auto_retry_total,
        )

    def _execute_with_retry(self, call: ToolCall) -> tuple[ToolResult, int]:
        """Retry observations, never silently replay an effect with an uncertain outcome."""
        retries = 0
        previous: ToolResult | None = None
        while True:
            if reason := dispatch_stop_reason(call, now=time.time()):
                return previous or skipped_before_dispatch(call, reason), retries
            if previous is not None:
                retries += 1
            try:
                raw = self.executor.execute(call)
                result = pair_executed_results([call], [raw])[0]
            except Exception:
                _LOG.exception("tool crashed: %s", call.name)
                return execution_unknown_result(call, "executor raised after dispatch"), retries
            error = result.error
            if (
                not result.is_error or not self.auto_retry_transient
                or error is None or error.retryable is False
                or not is_read_only_tool(call.name, self.registry)
            ):
                return result, retries
            verdict = classify_for_recovery(
                error_kind=error.kind.value, error_message=error.message,
            )
            policy = policy_for_kind(verdict.category)
            if policy.action != RecoveryAction.AUTO_RETRY or retries >= policy.max_retries:
                return result, retries
            if dispatch_stop_reason(call, now=time.time()):
                return result, retries
            delay = policy.backoff_for_attempt(retries + 1)
            deadline = call.metadata.get("turn_deadline_epoch")
            if deadline is not None and time.time() + delay >= float(deadline):
                return result, retries
            previous = result
            if delay > 0:
                token = call.metadata.get("cancel_token")
                if isinstance(token, CancelToken):
                    token.wait(delay)
                else:
                    time.sleep(delay)

    def _is_read_only(self, call: ToolCall) -> bool:
        descriptor = self.registry.find(call.name)
        return bool(
            descriptor and descriptor.is_concurrency_safe
            and is_read_only_tool(call.name, self.registry)
        )


__all__ = [
    "BatchResult",
    "ContextModifierSink",
    "ToolOrchestrator",
]
