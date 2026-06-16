"""Runtime feature flags.

This module defines the baseline runtime feature flags that operators can
toggle without redeploying.

Design rules:

- All flags default to ``True`` (the features have been verified through
  ``tests/test_openhuman_reference_plan.py`` and an HTTP smoke run). Operators
  can disable any flag without redeploying.
- Resolution order, first match wins:
    1. Environment variable ``NERYA_FF_<FLAG_UPPER>``
       (``1/true/on/yes`` -> enabled, ``0/false/off/no`` -> disabled).
    2. Workspace overrides file ``workspace/state/runtime_flags.json``
       (JSON object: ``{"runtime.capability_catalog_v2": false, ...}``).
    3. Built-in default in :data:`DEFAULTS`.
- The module never imports the agent kernel, trading subsystems, or the API
  server so it is safe to call from anywhere, including module top-levels.
- A small in-process cache is used so repeated checks during a single HTTP
  request do not re-read the JSON file. :func:`reset_cache` is provided for
  tests and the ``/runtime/flags/refresh`` operator action.

The dashboard and HTTP routes consult these flags via :func:`is_enabled` and
:func:`snapshot`. Disabling a flag does **not** remove the route; routes
return an operator-safe envelope explaining the feature is gated off so the
UI can render a clean "feature disabled" card instead of a 404.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


# ---------------------------------------------------------------------------
# Flag registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FlagSpec:
    """Declarative metadata for a single runtime flag."""

    key: str
    default: bool
    phase: str
    summary: str


# Canonical runtime feature flags.
DEFAULTS: tuple[FlagSpec, ...] = (
    FlagSpec(
        key="runtime.capability_catalog_v2",
        default=True,
        phase="phase1",
        summary="Unified runtime capability catalog (`/capabilities/catalog`).",
    ),
    FlagSpec(
        key="runtime.data_source_sync_state",
        default=True,
        phase="phase1",
        summary="Unified data source sync state (`/data-sources/status`).",
    ),
    FlagSpec(
        key="runtime.tool_result_compaction",
        default=True,
        phase="phase2",
        summary="TokenJuice-style tool result compaction before LLM context injection.",
    ),
    FlagSpec(
        key="runtime.evidence_vault",
        default=True,
        phase="phase3",
        summary="Trading Evidence Vault (`/evidence/*`).",
    ),
    FlagSpec(
        key="runtime.prompt_guard_review_queue",
        default=True,
        phase="phase4",
        summary="Prompt guard three-tier verdict + review queue (`/security/prompt_guard/*`).",
    ),
    FlagSpec(
        key="runtime.operator_profile",
        default=True,
        phase="phase4",
        summary="Operator preference profile (`/memory/profile/*`).",
    ),
    FlagSpec(
        key="runtime.e2e_artifact_capture",
        default=True,
        phase="phase5",
        summary="E2E verification artifact capture (`/ops/e2e/*`).",
    ),
    FlagSpec(
        key="runtime.native_tool_gating",
        default=True,
        phase="phase6",
        summary=(
            "Progressive native tool disclosure: keep a small always-on core "
            "and reveal specialized tool families when the matching skill is "
            "viewed (skill_view). Shrinks per-turn tool payload."
        ),
    ),
)


_BY_KEY: Dict[str, FlagSpec] = {f.key: f for f in DEFAULTS}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_CACHE: Dict[str, bool] = {}
_CACHE_TS: float = 0.0
_CACHE_TTL_S: float = 5.0


def _env_key(key: str) -> str:
    return "NERYA_FF_" + key.replace(".", "_").replace("-", "_").upper()


def _parse_bool(raw: Optional[str]) -> Optional[bool]:
    if raw is None:
        return None
    s = raw.strip().lower()
    if s in ("1", "true", "on", "yes", "y"):
        return True
    if s in ("0", "false", "off", "no", "n"):
        return False
    return None


def _workspace_root(client: Any) -> Path:
    """Best-effort workspace root resolution.

    Mirrors the convention used by ``nerya.evidence.store`` and
    ``nerya.data_sources.sync_state``: prefer ``client.workspace_root`` when
    present, fall back to ``./workspace``.
    """
    root: Optional[str] = None
    try:
        if client is not None and hasattr(client, "workspace_root"):
            cand = getattr(client, "workspace_root")
            root = cand() if callable(cand) else cand
    except Exception:
        root = None
    if not root:
        root = os.environ.get("NERYA_WORKSPACE") or "workspace"
    return Path(root)


def _overrides_path(client: Any) -> Path:
    return _workspace_root(client) / "state" / "runtime_flags.json"


def _load_overrides(client: Any) -> Dict[str, bool]:
    path = _overrides_path(client)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text("utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, bool] = {}
    for k, v in raw.items():
        if isinstance(v, bool):
            out[str(k)] = v
        else:
            parsed = _parse_bool(str(v))
            if parsed is not None:
                out[str(k)] = parsed
    return out


def _refresh_cache(client: Any) -> None:
    global _CACHE_TS
    overrides = _load_overrides(client)
    snap: Dict[str, bool] = {}
    for spec in DEFAULTS:
        env_val = _parse_bool(os.environ.get(_env_key(spec.key)))
        if env_val is not None:
            snap[spec.key] = env_val
            continue
        if spec.key in overrides:
            snap[spec.key] = overrides[spec.key]
            continue
        snap[spec.key] = spec.default
    with _LOCK:
        _CACHE.clear()
        _CACHE.update(snap)
        _CACHE_TS = time.time()


def _ensure_fresh(client: Any) -> None:
    if (time.time() - _CACHE_TS) > _CACHE_TTL_S or not _CACHE:
        _refresh_cache(client)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_enabled(client: Any, key: str) -> bool:
    """Return whether ``key`` is currently enabled.

    Unknown keys default to ``True`` to keep the helper additive.
    """
    if key not in _BY_KEY:
        return True
    _ensure_fresh(client)
    return _CACHE.get(key, _BY_KEY[key].default)


def snapshot(client: Any) -> Dict[str, Any]:
    """Return a dict suitable for the operator dashboard."""
    _ensure_fresh(client)
    flags = []
    for spec in DEFAULTS:
        flags.append({
            "key": spec.key,
            "phase": spec.phase,
            "summary": spec.summary,
            "default": spec.default,
            "enabled": _CACHE.get(spec.key, spec.default),
            "env_override": _env_key(spec.key),
        })
    counts = {
        "total": len(flags),
        "enabled": sum(1 for f in flags if f["enabled"]),
        "disabled": sum(1 for f in flags if not f["enabled"]),
    }
    return {"flags": flags, "counts": counts, "overrides_path": str(_overrides_path(client))}


def set_override(client: Any, key: str, enabled: Optional[bool]) -> Dict[str, Any]:
    """Persist an override into the workspace state file.

    Passing ``enabled=None`` clears any existing override for ``key``.
    """
    if key not in _BY_KEY:
        return {"ok": False, "error": "unknown_flag", "key": key}
    path = _overrides_path(client)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _load_overrides(client)
    if enabled is None:
        current.pop(key, None)
    else:
        current[key] = bool(enabled)
    path.write_text(json.dumps(current, indent=2, sort_keys=True), "utf-8")
    reset_cache()
    return {"ok": True, "key": key, "enabled": enabled, "path": str(path)}


def reset_cache() -> None:
    """Drop the in-process cache. Tests and operator refresh call this."""
    global _CACHE_TS
    with _LOCK:
        _CACHE.clear()
        _CACHE_TS = 0.0


def known_keys() -> Iterable[str]:
    return tuple(spec.key for spec in DEFAULTS)
