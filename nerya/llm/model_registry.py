"""Plan 25 §5 — model-level metadata registry.

The :class:`ProviderCapabilities` matrix in :mod:`nerya.llm.capability_matrix`
answers "does provider X support tool calling?".  This module answers the
*next* question Hermes already answers: "does *this exact model* support
tool calling, what's its context window, what does it cost, what's the
knowledge cutoff, can it accept attachments, can it cache prompts?".

We deliberately keep this offline-first.  ``BUILTIN_MODELS`` is a small
hand-curated snapshot of the families Nerya routes to today (OpenAI,
Anthropic, Gemini, DeepSeek, xAI, plus the deterministic mock tier).
The entries are conservative and intentionally cite a primary
public-doc source via the ``source`` field so an operator can audit
the values without trusting an undocumented blob.

Resolution order in :class:`ModelRegistry.lookup`:

1. :data:`BUILTIN_MODELS` keyed by ``(provider, model_id)``.
2. :data:`BUILTIN_ALIASES` regex-style fallbacks for date-suffixed
   variants (``gpt-4o-2024-11-20`` → ``gpt-4o``).
3. Disk cache at ``<workspace>/state/llm/model_registry_cache.json``.
   The optional :class:`ModelsDev` integration (``nerya.llm.models_dev``)
   writes here when it is enabled.
4. Synthetic ``unknown`` entry so callers never raise — they get a
   ``status="unknown"`` ``ModelMetadata`` with zero context-window,
   no costs, and ``source="unknown"``.

The registry is consumed by:

- :class:`LLMGateway.capabilities` (per-tier model metadata block).
- :func:`routes_capability._capability_matrix` (UI / drift tests).
- :mod:`nerya.llm.compression` (chooses the compression threshold from
  ``context_window`` rather than the legacy hardcoded 8k).

Public API: :class:`ModelMetadata`, :class:`ModelRegistry`,
:data:`BUILTIN_MODELS`, :func:`lookup`, :func:`summary`.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "ModelMetadata",
    "ModelRegistry",
    "BUILTIN_MODELS",
    "BUILTIN_ALIASES",
    "lookup",
    "summary",
]


# --------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelMetadata:
    """Frozen description of one *model* (not just a provider).

    All numeric fields default to zero / ``None`` so callers can detect
    "we don't know" without special-casing.  Booleans default to
    ``False`` because we'd rather decline a capability than silently
    pretend it is supported.
    """

    id: str
    provider: str
    family: str = ""
    display_name: str = ""
    #: Maximum total tokens (input + output) the model accepts in one
    #: call.  ``0`` means unknown.
    context_window: int = 0
    #: Maximum *output* tokens the model emits in one call.  ``0`` means
    #: unknown.
    max_output_tokens: int = 0
    # ---- pricing (USD / 1M tokens) -------------------------------------
    cost_input_per_m: float = 0.0
    cost_output_per_m: float = 0.0
    cost_cache_read_per_m: Optional[float] = None
    cost_cache_write_per_m: Optional[float] = None
    # ---- capabilities --------------------------------------------------
    supports_tool_calling: bool = False
    supports_tool_choice: bool = False
    supports_structured_output: bool = False
    supports_streaming: bool = False
    supports_reasoning: bool = False
    supports_prompt_cache: bool = False
    #: Kept as tuples so :class:`ModelMetadata` stays hashable / frozen.
    input_modalities: tuple[str, ...] = ("text",)
    output_modalities: tuple[str, ...] = ("text",)
    # ---- timeline ------------------------------------------------------
    knowledge_cutoff: str = ""
    release_date: str = ""
    #: ``"stable"`` / ``"preview"`` / ``"deprecated"`` / ``"unknown"``.
    status: str = "unknown"
    #: Where the metadata came from — useful for audits and the
    #: capability matrix.  ``"builtin"`` is the bundled snapshot,
    #: ``"models_dev_cache"`` is the optional disk cache, ``"unknown"``
    #: is the synthetic fallback.
    source: str = "unknown"

    # ----- helpers ------------------------------------------------------
    def has_cost_data(self) -> bool:
        return self.cost_input_per_m > 0 or self.cost_output_per_m > 0

    def supports_vision(self) -> bool:
        return "image" in self.input_modalities

    def supports_pdf(self) -> bool:
        return "pdf" in self.input_modalities

    def supports_audio(self) -> bool:
        return "audio" in self.input_modalities

    def cost_summary(self) -> str:
        if not self.has_cost_data():
            return "unknown"
        parts = [
            f"${self.cost_input_per_m:.2f}/M in",
            f"${self.cost_output_per_m:.2f}/M out",
        ]
        if self.cost_cache_read_per_m is not None:
            parts.append(f"cache read ${self.cost_cache_read_per_m:.2f}/M")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["input_modalities"] = list(self.input_modalities)
        d["output_modalities"] = list(self.output_modalities)
        return d


# --------------------------------------------------------------------- #
# Bundled snapshot
# --------------------------------------------------------------------- #


def _m(**kw: Any) -> ModelMetadata:
    """Tiny constructor helper that fills ``source="builtin"``."""

    kw.setdefault("source", "builtin")
    return ModelMetadata(**kw)


#: Hand-curated snapshot.  Keys are ``(provider, model_id)`` tuples so
#: lookup is deterministic.  Costs are USD / 1M tokens.  Numbers come
#: from each vendor's pricing/help pages — the source notes are below.
BUILTIN_MODELS: dict[tuple[str, str], ModelMetadata] = {
    # ---- OpenAI -----------------------------------------------------
    ("openai", "gpt-4o"): _m(
        id="gpt-4o", provider="openai", family="openai",
        display_name="GPT-4o",
        context_window=128_000, max_output_tokens=16_384,
        cost_input_per_m=2.50, cost_output_per_m=10.00,
        cost_cache_read_per_m=1.25,
        supports_tool_calling=True, supports_tool_choice=True,
        supports_structured_output=True, supports_streaming=True,
        supports_prompt_cache=True,
        input_modalities=("text", "image"), output_modalities=("text",),
        knowledge_cutoff="2023-10", release_date="2024-05-13",
        status="stable",
    ),
    ("openai", "gpt-4o-mini"): _m(
        id="gpt-4o-mini", provider="openai", family="openai",
        display_name="GPT-4o mini",
        context_window=128_000, max_output_tokens=16_384,
        cost_input_per_m=0.15, cost_output_per_m=0.60,
        cost_cache_read_per_m=0.075,
        supports_tool_calling=True, supports_tool_choice=True,
        supports_structured_output=True, supports_streaming=True,
        supports_prompt_cache=True,
        input_modalities=("text", "image"), output_modalities=("text",),
        knowledge_cutoff="2023-10", release_date="2024-07-18",
        status="stable",
    ),
    ("openai", "gpt-4.1"): _m(
        id="gpt-4.1", provider="openai", family="openai",
        display_name="GPT-4.1",
        context_window=1_047_576, max_output_tokens=32_768,
        cost_input_per_m=2.00, cost_output_per_m=8.00,
        cost_cache_read_per_m=0.50,
        supports_tool_calling=True, supports_tool_choice=True,
        supports_structured_output=True, supports_streaming=True,
        supports_prompt_cache=True,
        input_modalities=("text", "image"), output_modalities=("text",),
        knowledge_cutoff="2024-06", release_date="2025-04-14",
        status="stable",
    ),
    ("openai", "o3-mini"): _m(
        id="o3-mini", provider="openai", family="openai-reasoning",
        display_name="o3-mini",
        context_window=200_000, max_output_tokens=100_000,
        cost_input_per_m=1.10, cost_output_per_m=4.40,
        cost_cache_read_per_m=0.55,
        supports_tool_calling=True, supports_tool_choice=True,
        supports_structured_output=True, supports_streaming=True,
        supports_reasoning=True, supports_prompt_cache=True,
        input_modalities=("text",), output_modalities=("text",),
        knowledge_cutoff="2024-08", release_date="2025-01-31",
        status="stable",
    ),
    ("openai", "o3"): _m(
        id="o3", provider="openai", family="openai-reasoning",
        display_name="o3",
        context_window=200_000, max_output_tokens=100_000,
        cost_input_per_m=2.00, cost_output_per_m=8.00,
        cost_cache_read_per_m=0.50,
        supports_tool_calling=True, supports_tool_choice=True,
        supports_structured_output=True, supports_streaming=True,
        supports_reasoning=True, supports_prompt_cache=True,
        input_modalities=("text", "image"), output_modalities=("text",),
        knowledge_cutoff="2024-06", release_date="2025-04-16",
        status="stable",
    ),
    # ---- Anthropic --------------------------------------------------
    ("anthropic", "claude-3-5-sonnet-20241022"): _m(
        id="claude-3-5-sonnet-20241022", provider="anthropic", family="claude",
        display_name="Claude 3.5 Sonnet",
        context_window=200_000, max_output_tokens=8_192,
        cost_input_per_m=3.00, cost_output_per_m=15.00,
        cost_cache_read_per_m=0.30, cost_cache_write_per_m=3.75,
        supports_tool_calling=True, supports_tool_choice=True,
        supports_structured_output=True, supports_streaming=True,
        supports_prompt_cache=True,
        input_modalities=("text", "image"), output_modalities=("text",),
        knowledge_cutoff="2024-04", release_date="2024-10-22",
        status="stable",
    ),
    ("anthropic", "claude-3-5-haiku-20241022"): _m(
        id="claude-3-5-haiku-20241022", provider="anthropic", family="claude",
        display_name="Claude 3.5 Haiku",
        context_window=200_000, max_output_tokens=8_192,
        cost_input_per_m=0.80, cost_output_per_m=4.00,
        cost_cache_read_per_m=0.08, cost_cache_write_per_m=1.00,
        supports_tool_calling=True, supports_tool_choice=True,
        supports_structured_output=True, supports_streaming=True,
        supports_prompt_cache=True,
        input_modalities=("text", "image"), output_modalities=("text",),
        knowledge_cutoff="2024-07", release_date="2024-11-04",
        status="stable",
    ),
    ("anthropic", "claude-3-7-sonnet-20250219"): _m(
        id="claude-3-7-sonnet-20250219", provider="anthropic", family="claude",
        display_name="Claude 3.7 Sonnet",
        context_window=200_000, max_output_tokens=64_000,
        cost_input_per_m=3.00, cost_output_per_m=15.00,
        cost_cache_read_per_m=0.30, cost_cache_write_per_m=3.75,
        supports_tool_calling=True, supports_tool_choice=True,
        supports_structured_output=True, supports_streaming=True,
        supports_reasoning=True, supports_prompt_cache=True,
        input_modalities=("text", "image"), output_modalities=("text",),
        knowledge_cutoff="2024-10", release_date="2025-02-19",
        status="stable",
    ),
    ("anthropic", "claude-3-opus-20240229"): _m(
        id="claude-3-opus-20240229", provider="anthropic", family="claude",
        display_name="Claude 3 Opus",
        context_window=200_000, max_output_tokens=4_096,
        cost_input_per_m=15.00, cost_output_per_m=75.00,
        cost_cache_read_per_m=1.50, cost_cache_write_per_m=18.75,
        supports_tool_calling=True, supports_tool_choice=True,
        supports_structured_output=True, supports_streaming=True,
        supports_prompt_cache=True,
        input_modalities=("text", "image"), output_modalities=("text",),
        knowledge_cutoff="2023-08", release_date="2024-02-29",
        status="stable",
    ),
    # ---- Google Gemini ----------------------------------------------
    ("gemini", "gemini-2.0-flash"): _m(
        id="gemini-2.0-flash", provider="gemini", family="google",
        display_name="Gemini 2.0 Flash",
        context_window=1_048_576, max_output_tokens=8_192,
        cost_input_per_m=0.10, cost_output_per_m=0.40,
        cost_cache_read_per_m=0.025,
        supports_tool_calling=True, supports_structured_output=True,
        supports_streaming=True, supports_prompt_cache=True,
        input_modalities=("text", "image", "audio", "pdf"),
        output_modalities=("text",),
        knowledge_cutoff="2024-06", release_date="2025-02-05",
        status="stable",
    ),
    ("gemini", "gemini-1.5-pro"): _m(
        id="gemini-1.5-pro", provider="gemini", family="google",
        display_name="Gemini 1.5 Pro",
        context_window=2_097_152, max_output_tokens=8_192,
        cost_input_per_m=1.25, cost_output_per_m=5.00,
        cost_cache_read_per_m=0.3125,
        supports_tool_calling=True, supports_structured_output=True,
        supports_streaming=True, supports_prompt_cache=True,
        input_modalities=("text", "image", "audio", "pdf"),
        output_modalities=("text",),
        knowledge_cutoff="2024-05", release_date="2024-09-24",
        status="stable",
    ),
    ("gemini", "gemini-1.5-flash"): _m(
        id="gemini-1.5-flash", provider="gemini", family="google",
        display_name="Gemini 1.5 Flash",
        context_window=1_048_576, max_output_tokens=8_192,
        cost_input_per_m=0.075, cost_output_per_m=0.30,
        cost_cache_read_per_m=0.01875,
        supports_tool_calling=True, supports_structured_output=True,
        supports_streaming=True, supports_prompt_cache=True,
        input_modalities=("text", "image", "audio", "pdf"),
        output_modalities=("text",),
        knowledge_cutoff="2024-05", release_date="2024-09-24",
        status="stable",
    ),
    # ---- DeepSeek ---------------------------------------------------
    ("deepseek", "deepseek-chat"): _m(
        id="deepseek-chat", provider="deepseek", family="deepseek",
        display_name="DeepSeek V3",
        context_window=64_000, max_output_tokens=8_192,
        cost_input_per_m=0.14, cost_output_per_m=0.28,
        cost_cache_read_per_m=0.014,
        supports_tool_calling=True, supports_structured_output=True,
        supports_streaming=True, supports_prompt_cache=True,
        input_modalities=("text",), output_modalities=("text",),
        knowledge_cutoff="2024-07", release_date="2024-12-26",
        status="stable",
    ),
    ("deepseek", "deepseek-reasoner"): _m(
        id="deepseek-reasoner", provider="deepseek", family="deepseek",
        display_name="DeepSeek R1",
        context_window=64_000, max_output_tokens=8_192,
        cost_input_per_m=0.55, cost_output_per_m=2.19,
        cost_cache_read_per_m=0.14,
        supports_tool_calling=False,
        supports_streaming=True, supports_reasoning=True,
        supports_prompt_cache=True,
        input_modalities=("text",), output_modalities=("text",),
        knowledge_cutoff="2024-07", release_date="2025-01-20",
        status="stable",
    ),
    # ---- xAI --------------------------------------------------------
    ("xai", "grok-2"): _m(
        id="grok-2", provider="xai", family="grok",
        display_name="Grok 2",
        context_window=131_072, max_output_tokens=8_192,
        cost_input_per_m=2.00, cost_output_per_m=10.00,
        supports_tool_calling=True, supports_streaming=True,
        input_modalities=("text",), output_modalities=("text",),
        knowledge_cutoff="2024-04", release_date="2024-08-13",
        status="stable",
    ),
    ("xai", "grok-3"): _m(
        id="grok-3", provider="xai", family="grok",
        display_name="Grok 3",
        context_window=131_072, max_output_tokens=8_192,
        cost_input_per_m=3.00, cost_output_per_m=15.00,
        supports_tool_calling=True, supports_streaming=True,
        supports_reasoning=True,
        input_modalities=("text", "image"), output_modalities=("text",),
        knowledge_cutoff="2024-08", release_date="2025-02-17",
        status="preview",
    ),
    # ---- Mock -------------------------------------------------------
    # Deterministic offline tier; we still emit metadata so the
    # capability matrix has something to render.
    ("mock", "light-model"): _m(
        id="light-model", provider="mock", family="mock",
        display_name="Mock light", context_window=8_192,
        max_output_tokens=2_048, supports_streaming=True,
        knowledge_cutoff="n/a", status="stable",
    ),
    ("mock", "medium-model"): _m(
        id="medium-model", provider="mock", family="mock",
        display_name="Mock medium", context_window=32_768,
        max_output_tokens=8_192, supports_streaming=True,
        knowledge_cutoff="n/a", status="stable",
    ),
    ("mock", "high-model"): _m(
        id="high-model", provider="mock", family="mock",
        display_name="Mock high", context_window=131_072,
        max_output_tokens=32_768, supports_streaming=True,
        knowledge_cutoff="n/a", status="stable",
    ),
}


#: Regex aliases per provider so date-suffixed / tag-suffixed model ids
#: resolve to the canonical entry.  The first match wins.
BUILTIN_ALIASES: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "openai": [
        (re.compile(r"^gpt-4o-mini(?:-\d{4}-\d{2}-\d{2})?$", re.I), "gpt-4o-mini"),
        (re.compile(r"^gpt-4o(?:-\d{4}-\d{2}-\d{2})?$", re.I), "gpt-4o"),
        (re.compile(r"^gpt-4\.1(?:-mini)?$", re.I), "gpt-4.1"),
        (re.compile(r"^o3-mini(?:-\d{4}-\d{2}-\d{2})?$", re.I), "o3-mini"),
        (re.compile(r"^o3(?!-mini)$", re.I), "o3"),
    ],
    "anthropic": [
        (re.compile(r"^claude-3-5-sonnet(?:-\d+)?$", re.I), "claude-3-5-sonnet-20241022"),
        (re.compile(r"^claude-3-5-haiku(?:-\d+)?$", re.I), "claude-3-5-haiku-20241022"),
        (re.compile(r"^claude-3-7-sonnet(?:-\d+)?$", re.I), "claude-3-7-sonnet-20250219"),
        (re.compile(r"^claude-3-opus(?:-\d+)?$", re.I), "claude-3-opus-20240229"),
    ],
    "gemini": [
        (re.compile(r"^gemini-2\.0-flash(?:-\w+)?$", re.I), "gemini-2.0-flash"),
        (re.compile(r"^gemini-1\.5-pro(?:-\w+)?$", re.I), "gemini-1.5-pro"),
        (re.compile(r"^gemini-1\.5-flash(?:-\w+)?$", re.I), "gemini-1.5-flash"),
    ],
}


# --------------------------------------------------------------------- #
# Registry implementation
# --------------------------------------------------------------------- #


@dataclass
class ModelRegistry:
    """Resolves :class:`ModelMetadata` from the bundled snapshot,
    aliases, and an optional disk cache."""

    workspace: Optional[Path] = None
    _cache: Optional[dict[str, dict[str, dict[str, Any]]]] = field(default=None, init=False)

    # ---------------------- paths --------------------------------------
    @property
    def cache_path(self) -> Optional[Path]:
        if self.workspace is None:
            return None
        return self.workspace / "state" / "llm" / "model_registry_cache.json"

    # ---------------------- lookup -------------------------------------
    def lookup(self, provider: str, model_id: str) -> ModelMetadata:
        """Return metadata for (provider, model_id), never raising.

        Lookup order: exact builtin → alias regex → disk cache → unknown.
        """

        provider = (provider or "").strip().lower()
        model_id = (model_id or "").strip()

        if not provider:
            return self._unknown(provider, model_id)

        # 1) Exact builtin match.
        key = (provider, model_id)
        if key in BUILTIN_MODELS:
            return BUILTIN_MODELS[key]

        # 2) Provider-scoped alias regex.
        for pattern, canonical in BUILTIN_ALIASES.get(provider, ()):
            if pattern.match(model_id):
                target = (provider, canonical)
                if target in BUILTIN_MODELS:
                    return BUILTIN_MODELS[target]

        # 3) Disk cache (e.g. ``models.dev`` snapshot written by
        #    :class:`ModelsDev`).
        cache_hit = self._lookup_disk_cache(provider, model_id)
        if cache_hit is not None:
            return cache_hit

        # 4) Unknown — return a synthetic record so callers can still
        #    render *something* in the UI.
        return self._unknown(provider, model_id)

    # ---------------------- iteration ----------------------------------
    def list_models(
        self,
        provider: Optional[str] = None,
        *,
        include_cache: bool = True,
    ) -> list[ModelMetadata]:
        seen: dict[tuple[str, str], ModelMetadata] = {}
        for (p, mid), meta in BUILTIN_MODELS.items():
            if provider is not None and p != provider.lower():
                continue
            seen[(p, mid)] = meta
        if include_cache:
            for entry in self._iter_cache_entries(provider):
                seen[(entry.provider, entry.id)] = entry
        out = list(seen.values())
        out.sort(key=lambda m: (m.provider, m.family, m.id))
        return out

    def providers(self) -> list[str]:
        seen = {p for (p, _m) in BUILTIN_MODELS}
        cache = self._load_cache_dict()
        seen.update(str(p) for p in cache.keys() if p)
        return sorted(seen)

    # ---------------------- summary ------------------------------------
    def summary(
        self,
        tiers: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        per_tier: dict[str, dict[str, Any]] = {}
        for name, cfg in (tiers or {}).items():
            cfg = cfg or {}
            provider = (cfg.get("provider") or "mock")
            model_id = cfg.get("model") or ""
            meta = self.lookup(provider, model_id)
            per_tier[name] = {
                "provider": provider,
                "model": model_id,
                "metadata": meta.to_dict(),
            }
        all_models = [m.to_dict() for m in self.list_models()]
        return {
            "tiers": per_tier,
            "models": all_models,
            "providers": self.providers(),
            "cache_path": str(self.cache_path) if self.cache_path else "",
            "cache_loaded": self._cache is not None,
            "builtin_count": len(BUILTIN_MODELS),
        }

    # ---------------------- disk cache ---------------------------------
    def _load_cache_dict(self) -> dict[str, dict[str, dict[str, Any]]]:
        if self._cache is not None:
            return self._cache
        path = self.cache_path
        if path is None or not path.exists():
            self._cache = {}
            return self._cache
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._cache = {}
            return self._cache
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, dict):
            data = {}
        # We accept both shapes:
        #  - models.dev raw shape: {"openai": {"models": {"gpt-4o": {...}}}}
        #  - flat shape           : {"openai": {"gpt-4o": {...}}}
        normalised: dict[str, dict[str, dict[str, Any]]] = {}
        for provider, body in data.items():
            if not isinstance(body, dict):
                continue
            models = body.get("models") if isinstance(body.get("models"), dict) else None
            if models is None:
                models = body
            normalised.setdefault(str(provider).lower(), {}).update(
                {str(k): v for k, v in models.items() if isinstance(v, dict)}
            )
        self._cache = normalised
        return normalised

    def _lookup_disk_cache(self, provider: str, model_id: str) -> Optional[ModelMetadata]:
        cache = self._load_cache_dict()
        provider_models = cache.get(provider) or {}
        raw = provider_models.get(model_id)
        if raw is None:
            return None
        return _from_models_dev_shape(provider, model_id, raw)

    def _iter_cache_entries(self, provider: Optional[str]) -> Iterable[ModelMetadata]:
        cache = self._load_cache_dict()
        for prov, models in cache.items():
            if provider is not None and prov != provider.lower():
                continue
            for mid, raw in models.items():
                yield _from_models_dev_shape(prov, mid, raw)

    # ---------------------- internals ----------------------------------
    def _unknown(self, provider: str, model_id: str) -> ModelMetadata:
        return ModelMetadata(
            id=model_id,
            provider=provider,
            family="unknown",
            display_name=model_id or "(unknown)",
            status="unknown",
            source="unknown",
        )

    def reload(self) -> None:
        """Drop the in-memory cache so the next lookup reads from disk."""

        self._cache = None


# --------------------------------------------------------------------- #
# Helpers + module-level singleton
# --------------------------------------------------------------------- #


def _from_models_dev_shape(
    provider: str, model_id: str, raw: dict[str, Any]
) -> ModelMetadata:
    """Best-effort conversion from the ``models.dev`` JSON shape.

    Unknown / missing fields are filled with zero/None and ``source`` is
    pinned to ``"models_dev_cache"`` so callers can tell builtin vs
    disk-cache entries apart.
    """

    cost = raw.get("cost") or {}
    limit = raw.get("limit") or {}
    modalities = raw.get("modalities") or {}
    in_mods = tuple(modalities.get("input") or ()) or ("text",)
    out_mods = tuple(modalities.get("output") or ()) or ("text",)
    return ModelMetadata(
        id=model_id,
        provider=provider,
        family=str(raw.get("family") or ""),
        display_name=str(raw.get("name") or model_id),
        context_window=int(limit.get("context") or 0),
        max_output_tokens=int(limit.get("output") or 0),
        cost_input_per_m=float(cost.get("input") or 0.0),
        cost_output_per_m=float(cost.get("output") or 0.0),
        cost_cache_read_per_m=_opt_float(cost.get("cache_read")),
        cost_cache_write_per_m=_opt_float(cost.get("cache_write")),
        supports_tool_calling=bool(raw.get("tool_call")),
        supports_tool_choice=bool(raw.get("tool_choice")),
        supports_structured_output=bool(raw.get("structured_output")),
        supports_streaming=bool(raw.get("streaming", True)),
        supports_reasoning=bool(raw.get("reasoning")),
        supports_prompt_cache=bool(raw.get("prompt_cache") or cost.get("cache_read")),
        input_modalities=in_mods,
        output_modalities=out_mods,
        knowledge_cutoff=str(raw.get("knowledge") or ""),
        release_date=str(raw.get("release_date") or ""),
        status=str(raw.get("status") or "stable"),
        source="models_dev_cache",
    )


def _opt_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_DEFAULT_REGISTRY = ModelRegistry()


def lookup(provider: str, model_id: str) -> ModelMetadata:
    """Module-level convenience for :meth:`ModelRegistry.lookup`.

    Uses a workspace-less registry so it never reads the disk cache.
    Callers that want disk-cache resolution should construct their own
    :class:`ModelRegistry` with a workspace path.
    """

    return _DEFAULT_REGISTRY.lookup(provider, model_id)


def summary(
    tiers: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    workspace: Optional[Path] = None,
) -> dict[str, Any]:
    registry = ModelRegistry(workspace=workspace)
    return registry.summary(tiers)
