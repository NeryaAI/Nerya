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


def _coerce_mapping(value: Any, *, string_key: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        return {string_key: value}
    try:
        return dict(value)
    except Exception:
        return {string_key: str(value)}


@dataclass
class StrategyAgentTask:
    status: AgentTaskStatus
    prompt: str = ""
    session_key: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    attached_skills: list[str] = field(default_factory=list)
    reason: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    # None inherits configured defaults; [] explicitly selects no inputs/roles.
    sources: list[str] | None = None
    outputs: list[str] | None = None
    roles: list[str] | None = None
    include_trigger: bool | None = None
    path: str = ""

    def __post_init__(self) -> None:
        self.validate()
        for name in ("sources", "outputs", "roles"):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, list(value))
        if self.path:
            self.metadata = {**self.metadata, "path": self.path}

    def validate(self) -> None:
        if self.status not in {"dispatch", "skip", "error"}:
            raise ValueError(f"Unknown Agent task status: {self.status!r}")
        for name in ("sources", "outputs", "roles"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in value) or len(value) != len(set(value))):
                raise ValueError(f"Agent task {name} must be a unique list of identifiers or None")
        if self.include_trigger is not None and not isinstance(self.include_trigger, bool):
            raise ValueError("include_trigger must be boolean or None")
        if not isinstance(self.path, str) or len(self.path) > 200:
            raise ValueError("path must be a string of at most 200 characters")

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
        context: dict[str, Any] | None = None,
        sources: list[str] | None = None,
        outputs: list[str] | None = None,
        roles: list[str] | None = None,
        include_trigger: bool | None = None,
        path: str = "",
    ) -> "StrategyAgentTask":
        return cls(
            status="dispatch",
            prompt=str(prompt or ""),
            session_key=_coerce_mapping(session_key, string_key="key"),
            metadata=_coerce_mapping(metadata, string_key="value"),
            artifacts=[dict(a) for a in (artifacts or [])],
            attached_skills=[str(s) for s in (attached_skills or []) if str(s).strip()],
            reason=str(reason or ""),
            context=_coerce_mapping(context, string_key="value"),
            sources=sources, outputs=outputs, roles=roles,
            include_trigger=include_trigger, path=path,
        )

    @classmethod
    def skip(
        cls,
        reason: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> "StrategyAgentTask":
        return cls(
            status="skip",
            reason=str(reason or ""),
            metadata=_coerce_mapping(metadata, string_key="value"),
        )

    @classmethod
    def stop(cls, reason: str, *, path: str = "", metadata: dict[str, Any] | None = None) -> "StrategyAgentTask":
        """End this invocation; callers must return the result to the entrypoint.

        This does not disable the schedule or undo work already performed.
        """
        return cls(status="skip", reason=str(reason), path=path,
                   metadata=_coerce_mapping(metadata, string_key="value"))

    @classmethod
    def error(
        cls,
        reason: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> "StrategyAgentTask":
        return cls(
            status="error",
            reason=str(reason or ""),
            metadata=_coerce_mapping(metadata, string_key="value"),
        )

    @classmethod
    def from_value(cls, value: Any) -> "StrategyAgentTask":
        if isinstance(value, cls):
            value.validate()
            return value
        if isinstance(value, str):
            return cls.dispatch(prompt=value)
        if isinstance(value, dict):
            status = str(value.get("status") or "dispatch").strip().lower()
            if status not in {"dispatch", "skip", "error"}:
                return cls.error(f"Unknown Agent task status: {status!r}")
            return cls(
                status=status,  # type: ignore[arg-type]
                prompt=str(value.get("prompt") or value.get("text") or ""),
                session_key=_coerce_mapping(value.get("session_key"), string_key="key"),
                metadata=_coerce_mapping(value.get("metadata"), string_key="value"),
                artifacts=[dict(a) for a in (value.get("artifacts") or [])],
                attached_skills=[
                    str(s) for s in (value.get("attached_skills") or []) if str(s).strip()
                ],
                reason=str(value.get("reason") or ""),
                context=_coerce_mapping(value.get("context"), string_key="value"),
                sources=value.get("sources"), outputs=value.get("outputs"),
                roles=value.get("roles"), include_trigger=value.get("include_trigger"),
                path=value.get("path", ""),
            )
        if value is None:
            return cls.skip("strategy returned no agent task")
        return cls.error(f"unsupported agent task return: {type(value).__name__}")

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["AgentTaskStatus", "StrategyAgentTask"]
