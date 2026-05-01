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
  which mirrors coding-agent's two-phase behaviour.
* **Context modifier replay** — modifiers are applied *after* the
  whole batch resolves, in tool-call order. This keeps the post-state
  consistent regardless of the parallel scheduling decisions made by
  the executor.

The orchestrator does **not** know about the LLM, the gateway, or
the transcript — it is a pure batch dispatcher over an executor.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .executor import NativeToolExecutor
from .registry import ToolNotFoundError, ToolRegistry
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
        # Lazy import — keeps orchestrator usable in legacy contexts that
        # never installed nerya.agent.error_recovery (e.g. minimal tests).
        try:
            from ..agent.error_recovery import classify_for_recovery, policy_for_kind, RecoveryAction
            self._classify = classify_for_recovery
            self._policy_for_kind = policy_for_kind
            self._RecoveryAction = RecoveryAction
        except Exception:  # pragma: no cover - defensive
            self._classify = None
            self._policy_for_kind = None
            self._RecoveryAction = None

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

        def _run_with_retry(call: ToolCall) -> tuple[ToolResult, int]:
            """Execute ``call``; auto-retry transient failures with backoff.

            Returns ``(result, retries_consumed)``. The caller only sees
            the *final* result — interim failures are discarded so the
            transcript never carries duplicated tool_result blocks for
            the same tool_use_id.
            """

            attempt = 0
            retries_consumed = 0
            while True:
                try:
                    r = self.executor.execute(call)
                except Exception:
                    _LOG.exception("tool crashed: %s", call.name)
                    return (
                        ToolResult(
                            tool_use_id=call.id,
                            name=call.name,
                            is_error=True,
                            content=[],
                        ),
                        retries_consumed,
                    )
                if not r.is_error or not self.auto_retry_transient:
                    return r, retries_consumed
                if self._classify is None or self._policy_for_kind is None:
                    return r, retries_consumed
                err = r.error
                if err is None:
                    return r, retries_consumed
                try:
                    verdict = self._classify(
                        error_kind=err.kind.value if err.kind else None,
                        error_message=err.message,
                    )
                except Exception:
                    return r, retries_consumed
                policy = self._policy_for_kind(verdict.category)
                if (
                    self._RecoveryAction is None
                    or policy.action != self._RecoveryAction.AUTO_RETRY
                    or attempt >= policy.max_retries
                ):
                    return r, retries_consumed
                attempt += 1
                retries_consumed += 1
                wait_s = policy.backoff_for_attempt(attempt)
                _LOG.info(
                    "auto-retry %s (attempt %d/%d) — category=%s wait=%.2fs",
                    call.name, attempt, policy.max_retries, verdict.category, wait_s,
                )
                if wait_s > 0:
                    time.sleep(wait_s)

        # ----- read-only batch (parallel) ---------------------------------
        if ro_indices:
            workers = min(self.max_parallel, len(ro_indices))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tool-ro") as pool:
                fut_to_idx = {
                    pool.submit(_run_with_retry, ordered[i]): i for i in ro_indices
                }
                for fut in as_completed(fut_to_idx):
                    idx = fut_to_idx[fut]
                    try:
                        results[idx], retries_consumed = fut.result()
                        auto_retry_total += retries_consumed
                    except Exception:
                        _LOG.exception("RO tool crashed in batch: %s", ordered[idx].name)
                        results[idx] = ToolResult(
                            tool_use_id=ordered[idx].id,
                            name=ordered[idx].name,
                            is_error=True,
                            content=[],
                        )

        # ----- mutating batch (serial, original order) --------------------
        for idx in mutating_indices:
            try:
                results[idx], retries_consumed = _run_with_retry(ordered[idx])
                auto_retry_total += retries_consumed
            except Exception:
                _LOG.exception("mutating tool crashed: %s", ordered[idx].name)
                results[idx] = ToolResult(
                    tool_use_id=ordered[idx].id,
                    name=ordered[idx].name,
                    is_error=True,
                    content=[],
                )

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

    # ------------------------------------------------------------------ utils

    def _is_read_only(self, call: ToolCall) -> bool:
        try:
            d = self.registry.get(call.name)
        except ToolNotFoundError:
            return False
        return bool(d.read_only and d.is_concurrency_safe)


__all__ = [
    "BatchResult",
    "ContextModifierSink",
    "ToolOrchestrator",
]
