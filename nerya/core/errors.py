"""Typed exceptions. Catching `NeryaError` catches everything we raise."""

from __future__ import annotations


class NeryaError(Exception):
    """Base class."""


class ConfigError(NeryaError): ...
class WorkspaceError(NeryaError): ...
class SkillError(NeryaError): ...
class SkillNotFoundError(SkillError): ...
class SkillPermissionError(SkillError): ...
class SkillManifestError(SkillError): ...
class SkillActionError(SkillError): ...

class TriggerError(NeryaError): ...
class TriggerRouteError(TriggerError): ...
class TriggerValidationError(TriggerError): ...

class TradingError(NeryaError):
    """Trading-layer failure.

    ``ambiguous=True`` marks a venue-outcome-unknown failure (F4/B1:
    e.g. a timeout after a request that may have been accepted). The
    executor reads it to keep the order tracked + pollable instead of
    marking it rejected. Defaults to False (definitive failure) so all
    existing call sites keep their semantics.

    R3T3: ``not_found=True`` marks a definitive "no such order" answer
    from the venue (e.g. the ccxt adapter catching ``OrderNotFound``).
    The order poller reads it to advance the 4-strike ``lost`` counter
    without relying on fragile message substrings.
    """

    def __init__(
        self,
        *args,
        ambiguous: bool = False,
        not_found: bool = False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.ambiguous = bool(ambiguous)
        self.not_found = bool(not_found)
class IntentValidationError(TradingError): ...
class RiskRejection(TradingError):
    """Raised when Risk Gate decides `reject`. `escalate` does not raise —
    it returns a decision that the caller must inspect."""
    def __init__(self, reasons: list[str], decision: dict | None = None):
        super().__init__("risk_rejected:" + ",".join(reasons))
        self.reasons = reasons
        self.decision = decision or {}

class ApprovalPending(TradingError):
    def __init__(self, approval_id: str):
        super().__init__(f"approval_pending:{approval_id}")
        self.approval_id = approval_id


class LLMError(NeryaError): ...
class LLMTierDenied(LLMError): ...
class LLMTaskNotAllowed(LLMError): ...
class LLMScriptQuotaExceeded(LLMError): ...
class LLMStructuredOutputError(LLMError): ...
class LLMApprovalRequired(LLMError):
    def __init__(self, approval_id: str, task: str, tier: str):
        super().__init__(f"approval_required:{approval_id}:{task}:{tier}")
        self.approval_id = approval_id
        self.task = task
        self.tier = tier

class SecurityError(NeryaError): ...
class SecretNotFoundError(SecurityError): ...
class SecretAccessDenied(SecurityError): ...
class PolicyDenied(SecurityError): ...
class PromptInjectionDetected(SecurityError):
    def __init__(self, patterns: list[str], caller: str = ""):
        super().__init__(
            f"prompt_injection:{caller or 'unknown'}:" + ",".join(patterns)
        )
        self.patterns = patterns
        self.caller = caller

class ScriptError(NeryaError): ...
class ScriptNotApproved(ScriptError): ...
class ScriptSandboxViolation(ScriptError): ...

class EvolutionError(NeryaError): ...
class ProtectedScopeViolation(EvolutionError): ...
