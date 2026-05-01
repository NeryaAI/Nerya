"""Per-provider LLM adapter modules.

The old ``nerya.llm.providers`` module concentrated every adapter, its
shared plumbing, and the pricing table in one 800+ line file. Splitting
the module into this package means:

* ``_base`` owns the shared types + transport + retry/pricing helpers.
* Each provider lives in its own file (``openai``, ``anthropic``,
  ``gemini``, ``ollama``).
* ``nerya.llm.providers`` stays as a thin re-export so *all* existing
  imports (``from nerya.llm.providers import OpenAIAdapter``) keep
  working.

Adding a new provider is then a single file that imports from
``_base`` — no giant diff against ``providers.py`` required.
"""

from ._base import (
    ModelInfo,
    ProviderCallable,
    ProviderResult,
    Transport,
    UrllibTransport,
)
from .anthropic import AnthropicAdapter
from .bedrock import BedrockAdapter
from .gemini import GeminiAdapter
from .google_code_assist import GoogleCodeAssistAdapter
from .ollama import OllamaAdapter
from .openai import DEFAULT_BASE_URLS, OpenAIAdapter, OpenAICompatAdapter


def builtin_providers(transport: Transport | None = None) -> dict[str, ProviderCallable]:
    """Return a dict of provider-name → adapter callable.

    Used by :class:`nerya.llm.model_router.ModelRouter` to resolve
    ``llm.tiers.<t>.provider`` back to a live adapter without anyone
    having to know about individual classes.
    """
    t = transport or UrllibTransport()
    compat = OpenAICompatAdapter(transport=t)
    return {
        "openai":             OpenAIAdapter(transport=t),
        "anthropic":          AnthropicAdapter(transport=t),
        "gemini":             GeminiAdapter(transport=t),
        "ollama":             OllamaAdapter(transport=t),
        "bedrock":            BedrockAdapter(transport=t),
        "google_code_assist": GoogleCodeAssistAdapter(transport=t),
        "deepseek":           compat,
        "openrouter":         compat,
        "moonshot":           compat,
        "xai":                compat,
        "mistral":            compat,
        "together":           compat,
        "groq":               compat,
        "cerebras":           compat,
        "stepfun":            compat,
        "compat":             compat,
    }


__all__ = [
    "ProviderResult",
    "ModelInfo",
    "Transport",
    "UrllibTransport",
    "ProviderCallable",
    "OpenAIAdapter",
    "OpenAICompatAdapter",
    "AnthropicAdapter",
    "GeminiAdapter",
    "OllamaAdapter",
    "BedrockAdapter",
    "GoogleCodeAssistAdapter",
    "DEFAULT_BASE_URLS",
    "builtin_providers",
]
