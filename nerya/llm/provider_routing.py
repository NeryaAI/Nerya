"""Provider routing preferences (provider-routing compatibility).

The operator config lives in ``workspace/llm/provider_routing.json``.
OpenRouter and any OpenAI-compatible router that understands the
``provider`` request field can consume this structure directly — we
forward it verbatim on a request when the call's provider is
``openrouter`` (or any explicitly opted-in provider).

Accepted keys, mirroring OpenRouter's ``/v1/chat/completions`` ``provider``
object:

* ``sort``              — ``price`` | ``throughput`` | ``latency``
* ``only``              — list of allowed upstream providers
* ``ignore``            — list of denied upstream providers
* ``order``             — strict provider priority order
* ``require_parameters`` — bool: fail instead of dropping unsupported params
* ``data_collection``   — ``allow`` | ``deny``

We validate softly — unknown keys are preserved (forward-compat), but
we reject obviously wrong types so UI writes fail fast rather than
silently producing misrouted calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.atomic_write import atomic_write_text
from ..core.time import now_iso


_ALLOWED_SORTS = {"price", "throughput", "latency"}
_ALLOWED_DATA_COLLECTION = {"allow", "deny"}


def _path(workspace: Path) -> Path:
    return Path(workspace) / "llm" / "provider_routing.json"


def load(workspace: Path) -> dict[str, Any]:
    p = _path(workspace)
    if not p.exists():
        return {
            "updated_at": None,
            "default": {},
            "per_provider": {},
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {
            "updated_at": None,
            "default": {},
            "per_provider": {},
        }


def _normalize_section(section: Any) -> dict[str, Any]:
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError("provider routing section must be an object")
    out: dict[str, Any] = {}
    for key, value in section.items():
        k = str(key)
        if k == "sort":
            if value is None:
                continue
            v = str(value).lower()
            if v not in _ALLOWED_SORTS:
                raise ValueError(
                    f"sort must be one of {sorted(_ALLOWED_SORTS)}, got {value!r}"
                )
            out[k] = v
        elif k in ("only", "ignore", "order"):
            if value is None:
                continue
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"{k} must be an array of provider ids")
            out[k] = [str(x) for x in value if str(x)]
        elif k == "require_parameters":
            if value is None:
                continue
            if not isinstance(value, bool):
                raise ValueError("require_parameters must be a boolean")
            out[k] = value
        elif k == "data_collection":
            if value is None:
                continue
            v = str(value).lower()
            if v not in _ALLOWED_DATA_COLLECTION:
                raise ValueError(
                    f"data_collection must be one of "
                    f"{sorted(_ALLOWED_DATA_COLLECTION)}, got {value!r}"
                )
            out[k] = v
        else:
            out[k] = value
    return out


def save(
    workspace: Path,
    *,
    default: dict[str, Any] | None = None,
    per_provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    doc = {
        "updated_at": now_iso(),
        "default": _normalize_section(default or {}),
        "per_provider": {
            str(k): _normalize_section(v)
            for k, v in (per_provider or {}).items()
        },
    }
    atomic_write_text(_path(workspace), json.dumps(doc, indent=2))
    return doc


def resolved_for(workspace: Path, provider: str) -> dict[str, Any]:
    """Return the effective routing preferences for a call to ``provider``.

    Precedence: per_provider override > default. Unset keys remain unset
    so the caller can distinguish "no preference" from "explicitly
    empty list".
    """
    doc = load(workspace)
    base = dict(doc.get("default") or {})
    per = (doc.get("per_provider") or {}).get((provider or "").lower())
    if isinstance(per, dict):
        for k, v in per.items():
            base[k] = v
    return base


__all__ = ["load", "save", "resolved_for"]
