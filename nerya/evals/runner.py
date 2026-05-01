"""Eval runner.

The runner glues a :class:`EvalScenario`'s scripted transcript into
the live :class:`WorkspaceNativeAgentLoop`, executes one turn under
the operator's :class:`ToolRegistry` + :class:`ToolOrchestrator`, and
turns the resulting :class:`LoopOutcome` into an
:class:`EvalRunResult` with pass/fail verdicts.

The runner is *infrastructure*, not a test framework. It does not
import pytest, does not produce JUnit XML, and does not assume a CLI
entry-point. Callers (CI scripts, ad-hoc dashboards, MCP eval probes)
build their own driver around this class. That keeps the harness
useful in three flavours of consumer:

* Local interactive — call :meth:`EvalRunner.run_scenario` directly.
* Catalog regression — call :meth:`EvalRunner.run_many` against the
  scenario catalog (eg. :data:`SCENARIO_TEMPLATES`).
* Offline replay — wire the runner with a scratch workspace + mocked
  permission engine to replay historical transcripts.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from ..llm.gateway import LLMGateway
from ..tools.orchestrator import ToolOrchestrator
from ..tools.registry import ToolRegistry
from .scenario import EvalRunResult, EvalScenario, EvalVerdict, ToolCallExpectation
from .transcript_backend import TranscriptMockBackend


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Loop factory
# ---------------------------------------------------------------------------


LoopFactory = Callable[..., Any]
"""Factory returning an instance of
:class:`nerya.agent.loop.WorkspaceNativeAgentLoop` (or any object
exposing ``run(system=..., user_message=...)``). Imported lazily so
the evals package stays importable on hosts that don't have the agent
loop wired."""


def _default_loop_factory(
    *,
    gateway: LLMGateway,
    registry: ToolRegistry,
    orchestrator: ToolOrchestrator,
    config: Any,
) -> Any:
    """Default factory — imports the production loop on demand."""

    from ..agent.loop import LoopConfig, WorkspaceNativeAgentLoop

    return WorkspaceNativeAgentLoop(
        gateway=gateway,
        registry=registry,
        orchestrator=orchestrator,
        config=config or LoopConfig(),
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class EvalRunner:
    """Run :class:`EvalScenario` instances against the agent loop.

    Construct one runner per harness session — it caches the gateway
    backend patch but is otherwise stateless. The runner does not own
    the workspace; callers pass a pre-built :class:`LLMGateway`,
    :class:`ToolRegistry`, and :class:`ToolOrchestrator`.
    """

    gateway: LLMGateway
    registry: ToolRegistry
    orchestrator: ToolOrchestrator
    loop_factory: LoopFactory = field(default=_default_loop_factory)
    tier: str = "mock-script"
    """Tier name we override on the gateway. Any tier works — the
    runner monkey-patches :meth:`LLMGateway._resolve_messages_backend`
    to return our scripted backend regardless of the value the loop
    asks for. The tier is recorded in :class:`EvalRunResult.metadata`
    for debugging."""

    # ------------------------------------------------------------------ run

    def run_scenario(self, scenario: EvalScenario) -> EvalRunResult:
        scratch: dict[str, Any] = {"scenario_id": scenario.id}
        backend = TranscriptMockBackend(script=scenario.script)

        if scenario.setup is not None:
            scenario.setup(scratch)

        recorded_calls: list[dict[str, Any]] = []
        original_run_batch = self.orchestrator.run_batch

        def record_run_batch(calls):  # type: ignore[no-untyped-def]
            batch = original_run_batch(calls)
            try:
                for call, result in zip(calls, batch.results):
                    recorded_calls.append(
                        {
                            "name": call.name,
                            "input": dict(call.arguments),
                            "ok": not result.is_error,
                            "status": "error" if result.is_error else "ok",
                            "tool_use_id": call.id,
                            "elapsed_ms": float(result.elapsed_ms),
                        }
                    )
            except Exception:
                _LOG.exception("eval runner: failed to record batch results")
            return batch

        self.orchestrator.run_batch = record_run_batch  # type: ignore[assignment]
        loop = None
        try:
            loop = self._build_loop(scenario)
            with self._patched_backend(backend):
                started = time.time()
                started_iso = _iso_now()
                outcome = loop.run(
                    system=scenario.system,
                    user_message=scenario.user_message
                    if scenario.user_message
                    else "begin scenario",
                )
                duration_ms = int((time.time() - started) * 1000)

                verdict = self._evaluate(scenario, outcome, recorded_calls)
                result = EvalRunResult(
                    scenario_id=scenario.id,
                    started_at=started_iso,
                    finished_at=_iso_now(),
                    duration_ms=duration_ms,
                    verdict=verdict,
                    iterations=outcome.iterations,
                    stop_reason=outcome.stop_reason,
                    final_text=outcome.final_text,
                    tool_calls=list(recorded_calls),
                    transcript=list(outcome.transcript),
                    setup_state=dict(scratch),
                    metadata={
                        "tier": self.tier,
                        "script_cursor": backend.cursor,
                        "script_history": list(backend.history),
                        "abort_reason": outcome.abort_reason,
                    },
                )
                if scenario.custom_predicate is not None:
                    err = scenario.custom_predicate(result)
                    if err:
                        result.verdict.passed = False
                        result.verdict.failures.append(f"custom_predicate: {err}")
                return result
        except Exception as exc:
            _LOG.exception("eval runner: scenario %s crashed", scenario.id)
            return EvalRunResult(
                scenario_id=scenario.id,
                started_at=_iso_now(),
                finished_at=_iso_now(),
                duration_ms=0,
                verdict=EvalVerdict(
                    passed=False, failures=[f"runner_crashed: {exc}"]
                ),
                iterations=0,
                stop_reason="error",
                final_text="",
                tool_calls=list(recorded_calls),
                error=str(exc),
                setup_state=dict(scratch),
            )
        finally:
            self.orchestrator.run_batch = original_run_batch  # type: ignore[assignment]
            if scenario.teardown is not None:
                try:
                    scenario.teardown(scratch)
                except Exception:
                    _LOG.exception("eval runner: teardown for %s failed", scenario.id)

    def run_many(
        self,
        scenarios: Iterable[EvalScenario],
        *,
        stop_on_failure: bool = False,
    ) -> list[EvalRunResult]:
        results: list[EvalRunResult] = []
        for scenario in scenarios:
            result = self.run_scenario(scenario)
            results.append(result)
            if stop_on_failure and not result.verdict.passed:
                break
        return results

    # ------------------------------------------------------------------ helpers

    def _build_loop(self, scenario: EvalScenario):  # type: ignore[no-untyped-def]
        from ..agent.loop import LoopConfig

        loop_config = LoopConfig(
            max_iterations=max(8, len(scenario.script.turns) + 4),
            max_wall_seconds=scenario.timeout_seconds,
            tier=self.tier,
            task="evals.scenario",
            caller=f"evals:{scenario.id}",
        )
        return self.loop_factory(
            gateway=self.gateway,
            registry=self.registry,
            orchestrator=self.orchestrator,
            config=loop_config,
        )

    @contextmanager
    def _patched_backend(self, backend: TranscriptMockBackend):  # type: ignore[no-untyped-def]
        """Monkey-patch the gateway's backend resolver for one scenario.

        Restores the original method on exit. We don't touch the
        config — the loop asks for ``tier=self.tier`` and the patched
        resolver always returns ``backend`` regardless of input, so
        scenarios run deterministically without polluting the
        operator's tier table.
        """

        original = self.gateway._resolve_messages_backend  # type: ignore[attr-defined]

        def _patched(_tier_or_request: Any) -> TranscriptMockBackend:  # noqa: ANN001
            return backend

        self.gateway._resolve_messages_backend = _patched  # type: ignore[assignment]
        try:
            yield
        finally:
            self.gateway._resolve_messages_backend = original  # type: ignore[assignment]

    def _evaluate(
        self,
        scenario: EvalScenario,
        outcome: Any,
        recorded_calls: list[dict[str, Any]],
    ) -> EvalVerdict:
        failures: list[str] = []
        notes: list[str] = []

        if (
            scenario.expected_stop_reason
            and outcome.stop_reason != scenario.expected_stop_reason
        ):
            failures.append(
                f"stop_reason expected {scenario.expected_stop_reason!r}, "
                f"got {outcome.stop_reason!r}"
            )

        for fragment in scenario.expected_final_text_contains:
            if fragment and fragment not in (outcome.final_text or ""):
                failures.append(
                    f"final_text missing fragment {fragment!r}"
                )

        idx = 0
        for expected in scenario.expected_tool_calls:
            match = self._consume_match(expected, recorded_calls, idx)
            if match is None:
                failures.append(
                    f"missing expected tool call {expected.name!r} "
                    f"(input subset {expected.input_subset!r})"
                )
            else:
                idx = match + 1
                actual = recorded_calls[match]
                if expected.expected_status not in ("any", actual.get("status")):
                    failures.append(
                        f"tool {expected.name!r} expected status "
                        f"{expected.expected_status!r}, got "
                        f"{actual.get('status')!r}"
                    )

        if not scenario.allow_extra_calls:
            extras = [
                c["name"]
                for c in recorded_calls[idx:]
                if c.get("name")
            ]
            if extras:
                failures.append(f"unexpected tool calls: {extras!r}")

        passed = not failures
        return EvalVerdict(passed=passed, failures=failures, notes=notes)

    @staticmethod
    def _consume_match(
        expected: ToolCallExpectation,
        recorded: list[dict[str, Any]],
        start: int,
    ) -> Optional[int]:
        for i in range(start, len(recorded)):
            call = recorded[i]
            if expected.matches(
                str(call.get("name") or ""),
                dict(call.get("input") or {}),
            ):
                return i
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


__all__ = ["EvalRunner", "LoopFactory"]
