"""Best-effort lifecycle hooks that feed the self-evolution store."""

from __future__ import annotations

import logging
from typing import Any

from ..core.config import Config
from .events import EvolutionSignal
from .event_store import append_signal, record_event
from .quality import evaluate_learning_candidate


_LOG = logging.getLogger(__name__)


class EvolutionHookBus:
    """Small, failure-isolated hook surface used by AgentKernel."""

    def __init__(self, config: Config):
        self.config = config

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("agent.native.evolution_hooks_enabled", True))

    def after_tool_result(
        self,
        *,
        turn_id: str,
        session_id: str | None = None,
        strategy_id: str | None = None,
        tool: str = "",
        ok: bool = True,
        error: str | None = None,
        error_kind: str | None = None,
    ) -> None:
        if self.enabled and not ok:
            self._safe(
                lambda: append_signal(
                    self.config.paths,
                    EvolutionSignal.create(
                        source="tool",
                        kind="tool_failure_cluster",
                        severity="warn",
                        strategy_id=strategy_id,
                        evidence_refs=[f"turn:{turn_id}"],
                        summary=f"Tool/action {tool or 'unknown'} failed: {error or error_kind or 'unknown error'}",
                        dedupe_key=f"tool_failure:{tool or 'unknown'}:{strategy_id or '*'}",
                        confidence=0.7,
                        metadata={
                            "turn_id": turn_id,
                            "session_id": session_id,
                            "tool": tool,
                            "error_kind": error_kind,
                        },
                    ),
                    dedupe=True,
                )
            )

    def after_turn(self, *, turn_id: str, result: Any) -> None:
        if not self.enabled:
            return
        strategy_id = getattr(result, "strategy_id", None)
        session_id = getattr(result, "session_id", None)
        self._safe(
            lambda: record_event(
                self.config.paths,
                outcome="candidate",
                validation_status="not_run",
                strategy_id=strategy_id,
                summary=f"Agent turn {turn_id} completed.",
                evidence_refs=[f"turn:{turn_id}"],
                metadata={
                    "session_id": session_id,
                    "stopped_reason": getattr(result, "stopped_reason", None),
                    "tool_calls": (getattr(result, "budget", {}) or {}).get("tool_calls"),
                },
            )
        )

    def on_session_end(self, *, session_id: str, report: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self._safe(
            lambda: record_event(
                self.config.paths,
                outcome="candidate",
                validation_status="not_run",
                summary=f"Agent session {session_id} ended.",
                evidence_refs=[f"session:{session_id}"],
                strategy_id=report.get("strategy_id"),
                metadata={"reason": report.get("reason"), "turn_ids": report.get("turn_ids") or []},
            )
        )

    def on_memory_write(
        self,
        *,
        target: str,
        content: str,
        source: str,
        evidence_refs: list[str] | None = None,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        quality = evaluate_learning_candidate(content, evidence_refs=evidence_refs or [])
        if not self.enabled:
            return quality.asdict()
        if not quality.ok:
            self._safe(
                lambda: append_signal(
                    self.config.paths,
                    EvolutionSignal.create(
                        source="memory",
                        kind="memory_low_value_write",
                        severity="info",
                        strategy_id=strategy_id,
                        evidence_refs=evidence_refs or [],
                        summary=f"Memory write to {target} scored {quality.score}.",
                        dedupe_key=f"memory_low_value:{target}:{source}:{hash(content[:256])}",
                        confidence=1.0 - quality.score,
                        metadata={"target": target, "source": source, "quality": quality.asdict()},
                    ),
                    dedupe=True,
                )
            )
        return quality.asdict()

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception:
            _LOG.debug("evolution hook failed", exc_info=True)


__all__ = ["EvolutionHookBus"]
