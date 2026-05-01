"""Minimal models.dev integration.

``models.dev/api.json`` is a community-maintained registry of ~4000 LLM
models across 100+ providers, with rich metadata:

- context window, max output tokens
- per-million-token pricing (input, output, cache read/write)
- capabilities (reasoning, tool_call, attachment/vision, structured_output)
- knowledge cutoff, release date, deprecation status

This module provides a slim, cache-first client suitable for Nerya's
needs. It pairs with :class:`ModelCatalog` — provider ``/models``
endpoints give *the live list*, models.dev gives *the rich metadata*.

Resolution order:
    1. In-memory cache (TTL: 1 hour)
    2. Disk cache (``workspace/llm/models_dev_cache.json``)
    3. Network fetch (https://models.dev/api.json)
    4. Empty dict (best-effort; never raises)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .providers import Transport

_MODELS_DEV_URL = "https://models.dev/api.json"
_CACHE_TTL = 3600.0


@dataclass
class ModelsDevEntry:
    id: str
    provider_id: str
    name: str = ""
    family: str = ""
    context_window: int = 0
    max_output: int = 0
    cost_input_per_m: float = 0.0
    cost_output_per_m: float = 0.0
    cost_cache_read_per_m: float | None = None
    cost_cache_write_per_m: float | None = None
    reasoning: bool = False
    tool_call: bool = False
    attachment: bool = False
    structured_output: bool = False
    open_weights: bool = False
    knowledge_cutoff: str = ""
    release_date: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def cost_input_per_1k(self) -> float:
        return self.cost_input_per_m / 1000.0

    @property
    def cost_output_per_1k(self) -> float:
        return self.cost_output_per_m / 1000.0


@dataclass
class ModelsDev:
    workspace: Path
    transport: Transport | None = None
    ttl: float = _CACHE_TTL
    _mem: dict[str, Any] = field(default_factory=dict, init=False)
    _mem_ts: float = field(default=0.0, init=False)

    @property
    def cache_path(self) -> Path:
        return self.workspace / "llm" / "models_dev_cache.json"

    # ----------------------------------------------------------- load
    def load(self, *, force_refresh: bool = False) -> dict[str, Any]:
        now = time.time()
        if not force_refresh and self._mem and (now - self._mem_ts) < self.ttl:
            return self._mem

        if not force_refresh and self.cache_path.exists():
            try:
                doc = json.loads(self.cache_path.read_text(encoding="utf-8"))
                updated = float(doc.get("updated_at", 0))
                if (now - updated) < self.ttl:
                    self._mem = doc.get("data") or {}
                    self._mem_ts = updated
                    return self._mem
            except Exception:
                pass

        # fall through to network fetch
        return self.refresh()

    # ----------------------------------------------------------- refresh
    def refresh(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.transport is not None:
            try:
                status, body = self.transport.get_json(
                    _MODELS_DEV_URL, headers={}, timeout=20.0,
                )
                if 200 <= status < 300 and isinstance(body, dict):
                    data = body
            except Exception:
                data = {}

        if data:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"updated_at": time.time(), "data": data},
                            ensure_ascii=False),
                encoding="utf-8",
            )
            self._mem = data
            self._mem_ts = time.time()
        return data

    # ----------------------------------------------------------- lookup
    def provider_ids(self) -> list[str]:
        return list(self.load().keys())

    def get_model(self, provider_id: str, model_id: str) -> ModelsDevEntry | None:
        data = self.load()
        pdoc = data.get(provider_id) or {}
        models = (pdoc.get("models") or {})
        raw = models.get(model_id)
        if raw is None:
            return None
        return _entry_from_raw(provider_id, model_id, raw)

    def list_models(self, provider_id: str) -> list[ModelsDevEntry]:
        data = self.load()
        pdoc = data.get(provider_id) or {}
        models = (pdoc.get("models") or {})
        out: list[ModelsDevEntry] = []
        for mid, raw in models.items():
            out.append(_entry_from_raw(provider_id, mid, raw))
        return out


def _entry_from_raw(provider_id: str, model_id: str, raw: dict[str, Any]) -> ModelsDevEntry:
    cost = raw.get("cost") or {}
    limit = raw.get("limit") or {}
    modalities_in = tuple(raw.get("modalities", {}).get("input") or ())
    caps = raw
    return ModelsDevEntry(
        id=model_id,
        provider_id=provider_id,
        name=str(raw.get("name", "") or ""),
        family=str(raw.get("family", "") or ""),
        context_window=int(limit.get("context") or 0),
        max_output=int(limit.get("output") or 0),
        cost_input_per_m=float(cost.get("input") or 0.0),
        cost_output_per_m=float(cost.get("output") or 0.0),
        cost_cache_read_per_m=_opt_float(cost.get("cache_read")),
        cost_cache_write_per_m=_opt_float(cost.get("cache_write")),
        reasoning=bool(caps.get("reasoning")),
        tool_call=bool(caps.get("tool_call")),
        attachment=bool(caps.get("attachment")) or "image" in modalities_in,
        structured_output=bool(caps.get("structured_output")),
        open_weights=bool(caps.get("open_weights")),
        knowledge_cutoff=str(raw.get("knowledge") or ""),
        release_date=str(raw.get("release_date") or ""),
        raw=raw,
    )


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = ["ModelsDev", "ModelsDevEntry"]
