"""Compatibility shim — all adapters now live in ``nerya.llm.adapters``.

Historically this module held every LLM provider adapter, the shared
HTTP transport, the retry loop, and the pricing table in one ~830 line
file. That made it both hard to read *and* hard to extend — adding a
new provider meant touching an already-crowded file.

We split the module into a small package:

* :mod:`nerya.llm.adapters._base` — shared types, transport, retry,
  pricing.
* :mod:`nerya.llm.adapters.openai` / ``anthropic`` / ``gemini`` /
  ``ollama`` — one file per concrete adapter.
* :mod:`nerya.llm.adapters.__init__` — :func:`builtin_providers`
  registry.

This module stays as a **thin re-export** so every existing
``from nerya.llm.providers import OpenAIAdapter, builtin_providers``
import keeps working with zero churn in callers or tests.
"""

from __future__ import annotations

from .adapters import (
    DEFAULT_BASE_URLS,
    AnthropicAdapter,
    BedrockAdapter,
    GeminiAdapter,
    GoogleCodeAssistAdapter,
    ModelInfo,
    OllamaAdapter,
    OpenAIAdapter,
    OpenAICompatAdapter,
    ProviderCallable,
    ProviderResult,
    Transport,
    UrllibTransport,
    builtin_providers,
)

# Private helpers — still re-exported for legacy callers (e.g. tests that
# asserted on pattern-based pricing directly).
from .adapters._base import (  # noqa: F401  (re-export)
    _estimate_tokens,
    _post_with_retry,
    _price_for,
    _PRICE_PATTERNS,
    _rate_limit_key,
    _split_prompt_for_chat,
)

__all__ = [
    "ProviderResult",
    "ModelInfo",
    "Transport",
    "UrllibTransport",
    "OpenAIAdapter",
    "AnthropicAdapter",
    "GeminiAdapter",
    "OllamaAdapter",
    "OpenAICompatAdapter",
    "BedrockAdapter",
    "GoogleCodeAssistAdapter",
    "DEFAULT_BASE_URLS",
    "ProviderCallable",
    "builtin_providers",
]
