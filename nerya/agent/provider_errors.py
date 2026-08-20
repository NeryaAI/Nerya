"""Provider-error taxonomy at the compatibility boundary.

Provider adapters should eventually emit typed failures. Until every adapter
does, all message-based compatibility matching lives here rather than inside
the turn loop or recovery policy.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.errors import (
    LLMApprovalRequired,
    LLMError,
    LLMScriptQuotaExceeded,
    LLMStructuredOutputError,
    LLMTaskNotAllowed,
    LLMTierDenied,
)


_NON_RETRYABLE_LLM_ERRORS: tuple[type[Exception], ...] = (
    LLMTierDenied,
    LLMTaskNotAllowed,
    LLMScriptQuotaExceeded,
    LLMStructuredOutputError,
    LLMApprovalRequired,
)

_TRANSIENT_HINTS: tuple[str, ...] = (
    "(429)",
    "(500)",
    "(502)",
    "(503)",
    "(504)",
    "(522)",
    "(524)",
    "(529)",
    "backend request failed",
    "rate_limit",
    "rate-limit",
    "rate limit",
    "timeout",
    "timed out",
    "connection",
    "network",
    "unreachable",
    "temporarily unavailable",
    "temporarily busy",
    "server busy",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "remote end closed connection",
    "服务器短暂繁忙",
    "短暂繁忙",
    "稍后重试",
    "ECONN",
    "ETIMEDOUT",
    "EAI_AGAIN",
)

_CONTEXT_OVERFLOW_HINTS: tuple[str, ...] = (
    "context_length_exceeded",
    "context length",
    "context_length",
    "context window",
    "context_window",
    "maximum context",
    "max context",
    "prompt is too long",
    "prompt too long",
    "input is too long",
    "input too long",
    "too many tokens",
    "tokens exceed",
    "token count exceeds",
    "exceeds the maximum number of tokens",
    "exceeds model context",
    "exceeds context",
    "exceed context",
    "request too large",
    "request_too_large",
    "payload too large",
    "(413)",
    "reduce the length of the messages",
    "range of input length",
    "input length should be",
    "输入长度",
    "超过最大长度",
    "上下文长度",
    "超出模型",
    "超过模型",
)

_SAFETY_REJECTION_HINTS: tuple[str, ...] = (
    "不安全",
    "敏感内容",
    "内容安全",
    "safety",
    "unsafe",
    "sensitive content",
    "new_sensitive",
    "input_sensitive",
    "output_sensitive",
    "content policy",
    "moderation",
)


def is_context_overflow_error(exc: BaseException) -> bool:
    if not isinstance(exc, LLMError):
        return False
    if isinstance(exc, _NON_RETRYABLE_LLM_ERRORS):
        return False
    message = str(exc).lower()
    return any(hint in message for hint in _CONTEXT_OVERFLOW_HINTS)


def is_transient_error(exc: BaseException) -> bool:
    if not isinstance(exc, LLMError):
        return False
    if isinstance(exc, _NON_RETRYABLE_LLM_ERRORS):
        return False
    if is_context_overflow_error(exc):
        return False
    message = str(exc).lower()
    return any(hint.lower() in message for hint in _TRANSIENT_HINTS)


def is_safety_rejection(exc: BaseException) -> bool:
    if not isinstance(exc, LLMError):
        return False
    status_code = int(getattr(exc, "status_code", 0) or 0)
    if status_code not in {400, 403, 422}:
        return False
    message = str(exc).lower()
    return any(hint.lower() in message for hint in _SAFETY_REJECTION_HINTS)


def transcript_char_size(messages: list[dict[str, Any]]) -> int:
    """Rough JSON-character size used only for compaction progress checks."""

    try:
        return sum(
            len(json.dumps(message, ensure_ascii=False, default=str))
            for message in messages
        )
    except Exception:
        return sum(len(str(message)) for message in messages)


__all__ = [
    "is_context_overflow_error",
    "is_safety_rejection",
    "is_transient_error",
    "transcript_char_size",
]
