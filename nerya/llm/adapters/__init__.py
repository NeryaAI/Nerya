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

    The mapping is derived from :data:`nerya.llm.provider_catalog.PROVIDER_CATALOG`
    so new providers are wired automatically — Anthropic-shaped ones go
    to :class:`AnthropicAdapter` (with custom ``base_url`` support), and
    OpenAI-shaped ones go to :class:`OpenAICompatAdapter`.
    """
    # Local import to avoid a circular import between ``nerya.llm.adapters``
    # and ``nerya.llm.provider_catalog`` (which itself doesn't import any
    # adapter classes, but importing the package early at module load
    # would still drag in this dict).
    from ..provider_catalog import PROVIDER_CATALOG

    t = transport or UrllibTransport()
    compat = OpenAICompatAdapter(transport=t)
    anthropic_compat = AnthropicAdapter(transport=t)

    out: dict[str, ProviderCallable] = {
        "openai":             OpenAIAdapter(transport=t),
        "openai-codex":       OpenAIAdapter(transport=t),
        "anthropic":          anthropic_compat,
        "claude-code":        anthropic_compat,
        "anthropic-compat":   anthropic_compat,
        "gemini":             GeminiAdapter(transport=t),
        "ollama":             OllamaAdapter(transport=t),
        "bedrock":            BedrockAdapter(transport=t),
        "google_code_assist": GoogleCodeAssistAdapter(transport=t),
        "google-gemini-cli":  GoogleCodeAssistAdapter(transport=t),
    }

    # Wire every other catalogue entry through the appropriate compat
    # adapter based on its ``api_mode``. We don't override entries that
    # already have a custom adapter mapped above.
    for entry in PROVIDER_CATALOG:
        if entry.id in out:
            continue
        if entry.api_mode == "anthropic_messages":
            out[entry.id] = anthropic_compat
        elif entry.api_mode == "chat_completions":
            out[entry.id] = compat
    out["compat"] = compat
    return out


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
