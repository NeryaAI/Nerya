"""Classify exceptions + HTTP statuses into stable categories.

Every consumer — retry logic, journal writers, alerting — should use these
categories instead of checking concrete exception types or raw status codes.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import errors as _e


# ---------------------------------------------------------------- categories
CATEGORY_RETRYABLE = "retryable"        # 408/409/425/429/5xx / timeouts / connection
CATEGORY_RATE_LIMIT = "rate_limit"      # explicit 429
CATEGORY_AUTH = "auth"                  # 401/403, bad api key
CATEGORY_PERMISSION = "permission"      # skill/secret scope denial
CATEGORY_VALIDATION = "validation"      # bad payload, schema violation
CATEGORY_BUDGET = "budget"              # LLM budget / quota
CATEGORY_APPROVAL = "approval"          # approval required / pending
CATEGORY_RISK = "risk"                  # risk gate reject
CATEGORY_SECURITY = "security"          # prompt injection, sandbox
CATEGORY_NOT_FOUND = "not_found"        # 404 / missing resource
CATEGORY_CONFIG = "config"              # missing / bad config
CATEGORY_INTERNAL = "internal"          # anything else


ALL_CATEGORIES = (
    CATEGORY_RETRYABLE,
    CATEGORY_RATE_LIMIT,
    CATEGORY_AUTH,
    CATEGORY_PERMISSION,
    CATEGORY_VALIDATION,
    CATEGORY_BUDGET,
    CATEGORY_APPROVAL,
    CATEGORY_RISK,
    CATEGORY_SECURITY,
    CATEGORY_NOT_FOUND,
    CATEGORY_CONFIG,
    CATEGORY_INTERNAL,
)


@dataclass(frozen=True)
class Classification:
    category: str
    retryable: bool
    is_user_error: bool
    message: str


def classify_status(status: int | None) -> str | None:
    """Return a category for a raw HTTP status, or None if ambiguous."""
    if status is None:
        return None
    if status == 429:
        return CATEGORY_RATE_LIMIT
    if status in (408, 409, 425) or 500 <= status <= 599:
        return CATEGORY_RETRYABLE
    if status in (401, 403):
        return CATEGORY_AUTH
    if status == 404:
        return CATEGORY_NOT_FOUND
    if 400 <= status < 500:
        return CATEGORY_VALIDATION
    return None


def classify(exc: BaseException, *, status: int | None = None) -> Classification:
    """Map any exception to a :class:`Classification`.

    The optional ``status`` overrides the exception-based mapping when the
    caller has a more precise answer (e.g. from a parsed HTTP response).
    """
    # Status takes priority if it's a clear signal.
    if status is not None:
        cat = classify_status(status)
        if cat in (CATEGORY_RATE_LIMIT, CATEGORY_AUTH, CATEGORY_RETRYABLE,
                   CATEGORY_NOT_FOUND):
            return Classification(
                category=cat,
                retryable=cat in (CATEGORY_RETRYABLE, CATEGORY_RATE_LIMIT),
                is_user_error=cat in (CATEGORY_AUTH, CATEGORY_VALIDATION,
                                      CATEGORY_NOT_FOUND),
                message=f"http {status}",
            )

    # Permission subclasses of SecurityError first (narrower -> broader).
    if isinstance(exc, (_e.SecretAccessDenied, _e.SkillPermissionError)):
        return Classification(CATEGORY_PERMISSION, False, True, str(exc))
    if isinstance(exc, _e.SecretNotFoundError):
        return Classification(CATEGORY_NOT_FOUND, False, True, str(exc))
    if isinstance(exc, _e.PromptInjectionDetected):
        return Classification(CATEGORY_SECURITY, False, False, str(exc))
    if isinstance(exc, (_e.ScriptSandboxViolation, _e.SecurityError)):
        return Classification(CATEGORY_SECURITY, False, False, str(exc))
    if isinstance(exc, _e.LLMScriptQuotaExceeded):
        return Classification(CATEGORY_BUDGET, False, False, str(exc))
    if isinstance(exc, _e.LLMApprovalRequired):
        return Classification(CATEGORY_APPROVAL, False, False, str(exc))
    if isinstance(exc, _e.ApprovalPending):
        return Classification(CATEGORY_APPROVAL, False, False, str(exc))
    if isinstance(exc, _e.RiskRejection):
        return Classification(CATEGORY_RISK, False, False, str(exc))
    if isinstance(exc, (_e.LLMTierDenied, _e.LLMTaskNotAllowed)):
        return Classification(CATEGORY_PERMISSION, False, True, str(exc))
    if isinstance(exc, _e.LLMStructuredOutputError):
        return Classification(CATEGORY_VALIDATION, False, True, str(exc))
    if isinstance(exc, (_e.IntentValidationError, _e.TriggerValidationError,
                        _e.SkillManifestError)):
        return Classification(CATEGORY_VALIDATION, False, True, str(exc))
    if isinstance(exc, _e.SkillNotFoundError):
        return Classification(CATEGORY_NOT_FOUND, False, True, str(exc))
    if isinstance(exc, _e.ConfigError):
        return Classification(CATEGORY_CONFIG, False, True, str(exc))
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return Classification(CATEGORY_RETRYABLE, True, False, str(exc))
    if isinstance(exc, _e.LLMError):
        # Fallback for generic LLM errors; message usually contains the cause.
        msg = str(exc).lower()
        if "rate" in msg or "429" in msg:
            return Classification(CATEGORY_RATE_LIMIT, True, False, str(exc))
        if "401" in msg or "403" in msg or "unauthorized" in msg or "forbidden" in msg:
            return Classification(CATEGORY_AUTH, False, True, str(exc))
        if "timeout" in msg or "timed out" in msg:
            return Classification(CATEGORY_RETRYABLE, True, False, str(exc))
        return Classification(CATEGORY_INTERNAL, False, False, str(exc))

    return Classification(CATEGORY_INTERNAL, False, False,
                          f"{type(exc).__name__}: {exc}")
