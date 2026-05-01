"""Provider capability matrix.

We refuse to claim "provider X is supported" unless we can *declare*
what exactly we support on that provider. This module is the single
source of truth for:

* which capabilities each LLM provider exposes through Nerya, and
* at what support tier: ``supported`` (production ready),
  ``experimental`` (behind a flag / limited support), ``metadata-only``
  (we list the provider but the feature isn't wired up yet),
  ``unsupported``.

Every entry here must be evidence-backed: either (a) we exercise the
capability in an adapter unit test, or (b) the adapter explicitly
no-ops / raises for that capability so we can detect absence.

The router and the SDK read this matrix to:

* surface a capability summary to operators (``client.llm.capabilities()``);
* fail fast with an explicit ``LLMError`` when a caller requests a
  capability we've marked ``unsupported`` on the active provider;
* drive the per-provider smoke-test gate in
  :mod:`tests/test_provider_capability_matrix.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal


Tier = Literal["supported", "experimental", "metadata-only", "unsupported"]


CAPABILITIES: tuple[str, ...] = (
    "sync_call",
    "streaming",
    "tool_calling",
    "tool_choice",
    "schema_json_mode",
    "reasoning_thinking",
    "multimodal_input",
    "model_discovery",
    "auth_api_key",
    "auth_bearer",
    "auth_oauth",
    "auth_aws_sigv4",
    "timeout_retry",
    "pricing_metadata",
)


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declarative capability record for a single provider."""
    provider: str
    family: str
    tiers: dict[str, Tier]

    def supports(self, capability: str) -> bool:
        """Return True if the provider claims at least ``experimental``."""
        level = self.tiers.get(capability, "unsupported")
        return level in ("supported", "experimental")

    def strictly_supports(self, capability: str) -> bool:
        return self.tiers.get(capability) == "supported"

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "family": self.family,
            "tiers": dict(self.tiers),
        }


# --------------------------------------------------------------- matrix
# NOTE: the matrix is intentionally conservative. A capability is only
# marked ``supported`` when the adapter actually exercises it in a
# regression test. Additions require a backing test.
MATRIX: dict[str, ProviderCapabilities] = {
    "openai": ProviderCapabilities(
        provider="openai", family="openai",
        tiers={
            "sync_call":           "supported",
            "streaming":           "experimental",
            "tool_calling":        "experimental",
            "tool_choice":         "experimental",
            "schema_json_mode":    "supported",
            "reasoning_thinking":  "experimental",
            "multimodal_input":    "experimental",
            "model_discovery":     "supported",
            "auth_api_key":        "supported",
            "auth_bearer":         "supported",
            "auth_oauth":          "unsupported",
            "auth_aws_sigv4":      "unsupported",
            "timeout_retry":       "supported",
            "pricing_metadata":    "supported",
        },
    ),
    "anthropic": ProviderCapabilities(
        provider="anthropic", family="anthropic",
        tiers={
            "sync_call":           "supported",
            "streaming":           "experimental",
            "tool_calling":        "experimental",
            "tool_choice":         "experimental",
            "schema_json_mode":    "experimental",
            "reasoning_thinking":  "supported",
            "multimodal_input":    "experimental",
            "model_discovery":     "metadata-only",
            "auth_api_key":        "supported",
            "auth_bearer":         "unsupported",
            "auth_oauth":          "unsupported",
            "auth_aws_sigv4":      "unsupported",
            "timeout_retry":       "supported",
            "pricing_metadata":    "supported",
        },
    ),
    "gemini": ProviderCapabilities(
        provider="gemini", family="google",
        tiers={
            "sync_call":           "supported",
            "streaming":           "experimental",
            "tool_calling":        "experimental",
            "tool_choice":         "unsupported",
            "schema_json_mode":    "experimental",
            "reasoning_thinking":  "experimental",
            "multimodal_input":    "experimental",
            "model_discovery":     "metadata-only",
            "auth_api_key":        "supported",
            "auth_bearer":         "unsupported",
            "auth_oauth":          "unsupported",
            "auth_aws_sigv4":      "unsupported",
            "timeout_retry":       "supported",
            "pricing_metadata":    "supported",
        },
    ),
    "bedrock": ProviderCapabilities(
        provider="bedrock", family="aws",
        tiers={
            "sync_call":           "supported",
            "streaming":           "metadata-only",
            "tool_calling":        "experimental",
            "tool_choice":         "unsupported",
            "schema_json_mode":    "experimental",
            "reasoning_thinking":  "metadata-only",
            "multimodal_input":    "metadata-only",
            "model_discovery":     "metadata-only",
            "auth_api_key":        "unsupported",
            "auth_bearer":         "unsupported",
            "auth_oauth":          "unsupported",
            "auth_aws_sigv4":      "supported",
            "timeout_retry":       "supported",
            "pricing_metadata":    "supported",
        },
    ),
    "google_code_assist": ProviderCapabilities(
        provider="google_code_assist", family="google",
        tiers={
            "sync_call":           "experimental",
            "streaming":           "metadata-only",
            "tool_calling":        "metadata-only",
            "tool_choice":         "unsupported",
            "schema_json_mode":    "metadata-only",
            "reasoning_thinking":  "metadata-only",
            "multimodal_input":    "metadata-only",
            "model_discovery":     "metadata-only",
            "auth_api_key":        "unsupported",
            "auth_bearer":         "unsupported",
            "auth_oauth":          "supported",
            "auth_aws_sigv4":      "unsupported",
            "timeout_retry":       "supported",
            "pricing_metadata":    "metadata-only",
        },
    ),
    "ollama": ProviderCapabilities(
        provider="ollama", family="local",
        tiers={
            "sync_call":           "supported",
            "streaming":           "experimental",
            "tool_calling":        "metadata-only",
            "tool_choice":         "unsupported",
            "schema_json_mode":    "metadata-only",
            "reasoning_thinking":  "metadata-only",
            "multimodal_input":    "metadata-only",
            "model_discovery":     "supported",
            "auth_api_key":        "unsupported",
            "auth_bearer":         "unsupported",
            "auth_oauth":          "unsupported",
            "auth_aws_sigv4":      "unsupported",
            "timeout_retry":       "supported",
            "pricing_metadata":    "metadata-only",
        },
    ),
    # OpenAI-compatible providers share the same adapter and capability
    # profile; listing them explicitly so operators can see exactly which
    # families Nerya talks to.
    **{
        name: ProviderCapabilities(
            provider=name, family="openai_compat",
            tiers={
                "sync_call":           "supported",
                "streaming":           "experimental",
                "tool_calling":        "experimental",
                "tool_choice":         "metadata-only",
                "schema_json_mode":    "experimental",
                "reasoning_thinking":  "metadata-only",
                "multimodal_input":    "metadata-only",
                "model_discovery":     "metadata-only",
                "auth_api_key":        "supported",
                "auth_bearer":         "supported",
                "auth_oauth":          "unsupported",
                "auth_aws_sigv4":      "unsupported",
                "timeout_retry":       "supported",
                "pricing_metadata":    "metadata-only",
            },
        )
        for name in ("deepseek", "openrouter", "moonshot", "xai",
                     "mistral", "together", "groq", "cerebras", "stepfun", "compat")
    },
    # "mock" always exists — it is deterministic and offline. We mark it
    # as metadata-only on everything that implies a real provider; callers
    # should not conflate mock tier support with live capability.
    "mock": ProviderCapabilities(
        provider="mock", family="mock",
        tiers={
            **{c: "metadata-only" for c in CAPABILITIES},
            "sync_call":      "supported",
            "timeout_retry":  "supported",
            "pricing_metadata": "supported",
        },
    ),
}


class CapabilityError(Exception):
    """Raised when a caller asks for a capability the provider lacks."""


def capability_of(provider: str) -> ProviderCapabilities:
    key = (provider or "").lower()
    if key not in MATRIX:
        return ProviderCapabilities(
            provider=key or "unknown", family="unknown",
            tiers={c: "unsupported" for c in CAPABILITIES},
        )
    return MATRIX[key]


def summary() -> list[dict[str, object]]:
    """Return the matrix as a JSON-ready list for dashboards / MCP."""
    return [MATRIX[name].as_dict() for name in sorted(MATRIX)]


def require(provider: str, capability: str) -> None:
    """Raise :class:`CapabilityError` when a provider lacks a capability."""
    cap = capability_of(provider)
    level = cap.tiers.get(capability, "unsupported")
    if level == "unsupported":
        raise CapabilityError(
            f"provider {provider!r} does not support capability "
            f"{capability!r} (matrix: {level!r})"
        )


def iter_providers() -> Iterable[str]:
    return MATRIX.keys()


__all__ = [
    "CAPABILITIES",
    "ProviderCapabilities",
    "MATRIX",
    "CapabilityError",
    "capability_of",
    "summary",
    "require",
    "iter_providers",
]
