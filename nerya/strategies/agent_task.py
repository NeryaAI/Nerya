"""Strategy-authored Agent task contract.

Strategy Agent tasks are the prompt-driven counterpart to the older
``run(ctx)`` tick contract. A strategy script gathers its own data,
computes indicators/factors, formats the final prompt, and returns one
of these envelopes. The runtime only dispatches the prompt into the
resolved Agent session; it does not infer indicator semantics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


AgentTaskStatus = Literal["dispatch", "skip", "error"]


@dataclass
class StrategyAgentTask:
    status: AgentTaskStatus
    prompt: str = ""
    session_key: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    attached_skills: list[str] = field(default_factory=list)
    reason: str = ""

    @classmethod
    def dispatch(
        cls,
        *,
        prompt: str,
        session_key: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        attached_skills: list[str] | None = None,
        reason: str = "",
    ) -> "StrategyAgentTask":
        return cls(
            status="dispatch",
            prompt=str(prompt or ""),
            session_key=dict(session_key or {}),
            metadata=dict(metadata or {}),
            artifacts=[dict(a) for a in (artifacts or [])],
            attached_skills=[str(s) for s in (attached_skills or []) if str(s).strip()],
            reason=str(reason or ""),
        )

    @classmethod
    def skip(
        cls,
        reason: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> "StrategyAgentTask":
        return cls(status="skip", reason=str(reason or ""), metadata=dict(metadata or {}))

    @classmethod
    def error(
        cls,
        reason: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> "StrategyAgentTask":
        return cls(status="error", reason=str(reason or ""), metadata=dict(metadata or {}))

    @classmethod
    def from_value(cls, value: Any) -> "StrategyAgentTask":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls.dispatch(prompt=value)
        if isinstance(value, dict):
            status = str(value.get("status") or "dispatch").strip().lower()
            if status not in {"dispatch", "skip", "error"}:
                status = "dispatch"
            return cls(
                status=status,  # type: ignore[arg-type]
                prompt=str(value.get("prompt") or value.get("text") or ""),
                session_key=dict(value.get("session_key") or {}),
                metadata=dict(value.get("metadata") or {}),
                artifacts=[dict(a) for a in (value.get("artifacts") or [])],
                attached_skills=[
                    str(s) for s in (value.get("attached_skills") or []) if str(s).strip()
                ],
                reason=str(value.get("reason") or ""),
            )
        if value is None:
            return cls.skip("strategy returned no agent task")
        return cls.error(f"unsupported agent task return: {type(value).__name__}")

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["AgentTaskStatus", "StrategyAgentTask"]
