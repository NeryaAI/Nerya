"""Per-run LLM quota tracking for scripts and subagents.

A session embodies the `llm_policy` declared in a script manifest (or the
default for a subagent / sdk caller) and counts calls/tokens/cost as calls
go through the gateway. The runtime creates a session per script_run_id
and injects it via `SkillCallContext.extras["llm_session"]`.

`active_session` is a contextvar so scripts running inside
`ScriptRunner.run_script(...)` automatically have their session resolved
by `SkillRuntime.call(...)` without threading it manually through every
SDK surface.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional

from ..core.errors import LLMScriptQuotaExceeded, LLMTierDenied, LLMTaskNotAllowed

_active_session: ContextVar[Optional["LLMSession"]] = ContextVar(
    "nerya_active_llm_session", default=None,
)


def get_active_session() -> Optional["LLMSession"]:
    return _active_session.get()


def set_active_session(session: Optional["LLMSession"]):
    return _active_session.set(session)


def reset_active_session(token):
    _active_session.reset(token)


@dataclass
class LLMPolicy:
    """Subset of ScriptManifest.LLMPolicy reshaped for gateway use."""
    allowed_tiers: list[str] = field(default_factory=lambda: ["light"])
    allowed_tasks: list[str] = field(default_factory=list)   # empty = any
    #: Capability families a caller is allowed to invoke. When set,
    #: a task also passes ``check_task`` if its normalised class is
    #: in this list. Additive with ``allowed_tasks``.
    allowed_classes: list[str] = field(default_factory=list)
    max_calls_per_run: int = 5
    max_tokens_per_run: int = 4000
    max_cost_usd_per_run: float = 1.0
    high_tier_requires_approval: bool = True


@dataclass
class LLMSession:
    caller: str
    policy: LLMPolicy
    calls_used: int = 0
    tokens_used: int = 0
    cost_used: float = 0.0

    # ------ pre-flight gates ---------------------------------------------
    def check_tier(self, tier: str) -> None:
        if self.policy.allowed_tiers and tier not in self.policy.allowed_tiers:
            raise LLMTierDenied(
                f"caller '{self.caller}' cannot use tier '{tier}' "
                f"(allowed={self.policy.allowed_tiers})"
            )

    def check_task(self, task: str) -> None:
        allowed = self.policy.allowed_tasks
        allowed_classes = self.policy.allowed_classes
        if not allowed and not allowed_classes:
            return
        if task in allowed:
            return
        if allowed_classes:
            from .task_classes import normalise_task_class
            cls = normalise_task_class(task)
            if cls and cls in allowed_classes:
                return
        raise LLMTaskNotAllowed(
            f"caller '{self.caller}' cannot invoke task '{task}' "
            f"(allowed_tasks={allowed}, allowed_classes={allowed_classes})"
        )

    def check_quota_before(self) -> None:
        if self.policy.max_calls_per_run > 0 and \
           self.calls_used >= self.policy.max_calls_per_run:
            raise LLMScriptQuotaExceeded(
                f"caller '{self.caller}' exhausted max_calls_per_run="
                f"{self.policy.max_calls_per_run}"
            )

    def check_quota_after(self, *, tokens: int, cost: float) -> None:
        new_tokens = self.tokens_used + tokens
        new_cost = self.cost_used + cost
        if self.policy.max_tokens_per_run > 0 and new_tokens > self.policy.max_tokens_per_run:
            raise LLMScriptQuotaExceeded(
                f"caller '{self.caller}' exceeded max_tokens_per_run="
                f"{self.policy.max_tokens_per_run} (attempted {new_tokens})"
            )
        if self.policy.max_cost_usd_per_run > 0 and new_cost > self.policy.max_cost_usd_per_run:
            raise LLMScriptQuotaExceeded(
                f"caller '{self.caller}' exceeded max_cost_usd_per_run="
                f"{self.policy.max_cost_usd_per_run:.3f} (attempted {new_cost:.3f})"
            )

    def record(self, *, tokens: int, cost: float) -> None:
        self.calls_used += 1
        self.tokens_used += int(tokens)
        self.cost_used += float(cost)

    def snapshot(self) -> dict:
        return {
            "caller": self.caller,
            "calls_used": self.calls_used,
            "tokens_used": self.tokens_used,
            "cost_used": round(self.cost_used, 5),
            "allowed_tiers": list(self.policy.allowed_tiers),
            "allowed_tasks": list(self.policy.allowed_tasks),
            "allowed_classes": list(self.policy.allowed_classes),
            "max_calls_per_run": self.policy.max_calls_per_run,
            "max_tokens_per_run": self.policy.max_tokens_per_run,
            "max_cost_usd_per_run": self.policy.max_cost_usd_per_run,
        }


def session_from_script_manifest(caller: str, manifest_policy) -> LLMSession:
    """Adapt nerya.scripts.manifest.LLMPolicy into an LLMSession."""
    policy = LLMPolicy(
        allowed_tiers=list(manifest_policy.allowed_tiers),
        allowed_tasks=list(manifest_policy.allowed_tasks),
        allowed_classes=list(getattr(manifest_policy, "allowed_classes", []) or []),
        max_calls_per_run=int(manifest_policy.max_calls_per_run),
        max_tokens_per_run=int(manifest_policy.max_tokens_per_run),
        max_cost_usd_per_run=float(manifest_policy.max_cost_usd_per_day),
        high_tier_requires_approval=bool(manifest_policy.high_tier_requires_approval),
    )
    return LLMSession(caller=caller, policy=policy)
