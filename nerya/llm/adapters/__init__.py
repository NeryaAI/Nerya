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

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

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
    # Operator/workspace-contributed providers (see register_custom_provider)
    # are merged last so they can fill catalogue gaps but never shadow the
    # adapters explicitly wired above.
    for name, factory in list(_custom_provider_factories()):
        try:
            out[name] = _invoke_factory(factory, t)
        except Exception:  # a broken contributor must not break the router
            _ADAPTERS_LOG.warning("custom provider %r factory failed", name,
                                  exc_info=True)
    return out


# ---------------------------------------------------------------------------
# Contribution port (the "Provider" role of the LLM capability seam)
# ---------------------------------------------------------------------------

_CUSTOM_PROVIDERS: dict[str, Any] = {}
_CUSTOM_LOCK = threading.Lock()
_ADAPTERS_LOG = logging.getLogger("nerya.llm.adapters")


def _custom_provider_factories() -> list[tuple[str, Any]]:
    with _CUSTOM_LOCK:
        return sorted(_CUSTOM_PROVIDERS.items())


def _invoke_factory(factory: Any, transport: Transport | None) -> ProviderCallable:
    """Call ``factory`` with the shared transport, tolerating 0-arg factories."""

    import inspect

    try:
        takes_args = bool(inspect.signature(factory).parameters)
    except (TypeError, ValueError):
        takes_args = True
    return factory(transport) if takes_args else factory()


def register_custom_provider(
    name: str,
    factory: Any,
    *,
    replace: bool = False,
) -> Callable[[], None]:
    """Contribute an LLM provider resolvable by ``llm.tiers.<t>.provider``.

    ``factory`` is called as ``factory(transport)`` (zero-argument
    factories are accepted) and must return a
    :data:`ProviderCallable`. Registration takes effect for every
    ``ModelRouter`` / gateway constructed afterwards — the table is
    merged by :func:`builtin_providers` on each call.

    Returns a disposer that removes the contribution (the same
    registration-as-effect contract as ``nerya.harness.extensions``).

    This is the seam workspace plugins use via
    ``PluginContext.register_llm_provider``; before it existed, adding
    a provider meant editing the hardcoded map in
    ``builtin_providers``.
    """

    if not isinstance(name, str) or not name:
        raise ValueError("provider name must be a non-empty string")
    if not callable(factory):
        raise TypeError("provider factory must be callable")
    with _CUSTOM_LOCK:
        if name in _CUSTOM_PROVIDERS and not replace:
            raise ValueError(
                f"custom provider {name!r} already registered "
                "(pass replace=True to overwrite)"
            )
        _CUSTOM_PROVIDERS[name] = factory

    def _dispose() -> None:
        with _CUSTOM_LOCK:
            _CUSTOM_PROVIDERS.pop(name, None)

    return _dispose


def custom_providers() -> dict[str, Any]:
    """Snapshot of the currently registered custom provider factories."""

    return dict(_custom_provider_factories())


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
    "register_custom_provider",
    "custom_providers",
]
