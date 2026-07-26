"""Operator Preference Profile.

Persistent record of operator preferences that can prefill drafts and
recommendations. The agent self-learning profile is the
inspiration; the Nerya version preserves an explicit safety boundary:

  Preferences can prefill drafts and recommendations. Preferences
  CANNOT mutate live trading enabled, risk limits, approval policy,
  account permissions, vault secrets, or strategy promotion stage.

Storage::

    workspace/memory/operator_profile.jsonl

Each line is a fact::

    {
      "id": "fact_...",
      "ts": "...",
      "facet": "style" | "tooling" | "universe" | "risk_preference"
              | "veto" | "channel",
      "key": "preferred_language",
      "value": "en",
      "scope": "global" | "strategy:<id>" | "session:<id>",
      "pinned": false,
      "forgotten": false,
      "source": "agent_inferred" | "operator_set",
      "operator_id": "operator"
    }

The :class:`OperatorProfile` accessor exposes ``list_facts``, ``pin``,
``forget``, ``rebuild`` (cache rebuild), and ``stats``. It refuses to
touch trading-control fields under any circumstance.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..core import jsonl


VALID_FACETS: frozenset[str] = frozenset({
    "style", "tooling", "universe", "risk_preference", "veto", "channel",
})


# Hard safety boundary: keys in any facet that the profile must REFUSE to
# create or mutate even if asked. These keep "advisory preferences" from
# silently becoming runtime config.
_FORBIDDEN_KEYS: frozenset[str] = frozenset({
    "live_trading_enabled",
    "risk.max_drawdown_usd",
    "risk.max_open_positions",
    "approval.policy",
    "kill_switch",
    "vault://",  # any vault ref shape
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _profile_file(paths) -> Path:
    return paths.memory / "operator_profile.jsonl"


def _is_forbidden(key: str) -> bool:
    k = (key or "").strip()
    if not k:
        return True
    for forbidden in _FORBIDDEN_KEYS:
        if k == forbidden or k.startswith(forbidden):
            return True
    return False


def list_facts(
    paths,
    *,
    facet: Optional[str] = None,
    scope: Optional[str] = None,
    include_forgotten: bool = False,
) -> list[dict[str, Any]]:
    rows = jsonl.read_all(_profile_file(paths))
    out: list[dict[str, Any]] = []
    for row in rows:
        if not include_forgotten and row.get("forgotten"):
            continue
        if facet and row.get("facet") != facet:
            continue
        if scope and row.get("scope") != scope:
            continue
        out.append(row)
    return out


def set_fact(
    paths,
    *,
    facet: str,
    key: str,
    value: Any,
    scope: str = "global",
    pinned: bool = False,
    source: str = "operator_set",
    operator_id: str = "operator",
) -> dict[str, Any]:
    if facet not in VALID_FACETS:
        raise ValueError(
            f"invalid facet={facet!r}; expected one of {sorted(VALID_FACETS)}"
        )
    if _is_forbidden(str(key)):
        raise PermissionError(
            f"profile key {key!r} is part of the trading safety boundary "
            "and cannot be set via the operator profile."
        )
    fid = "fact_" + secrets.token_hex(6)
    rec = {
        "id": fid,
        "ts": _now_iso(),
        "facet": facet,
        "key": str(key),
        "value": value,
        "scope": scope,
        "pinned": bool(pinned),
        "forgotten": False,
        "source": source,
        "operator_id": operator_id,
    }
    path = _profile_file(paths)
    return jsonl.append(path, rec, stamp=False)


def pin(paths, *, fact_id: str) -> dict[str, Any]:
    path = _profile_file(paths)
    rows = jsonl.read_all(path)
    found = None
    for row in rows:
        if row.get("id") == fact_id:
            row["pinned"] = True
            found = row
            break
    if found is None:
        raise KeyError(f"fact {fact_id!r} not found")
    jsonl.write_all(path, rows)
    return found


def forget(paths, *, fact_id: str) -> dict[str, Any]:
    path = _profile_file(paths)
    rows = jsonl.read_all(path)
    found = None
    for row in rows:
        if row.get("id") == fact_id:
            row["forgotten"] = True
            row["forgotten_at"] = _now_iso()
            found = row
            break
    if found is None:
        raise KeyError(f"fact {fact_id!r} not found")
    jsonl.write_all(path, rows)
    return found


def rebuild_cache(paths) -> dict[str, Any]:
    """Re-read the journal and emit a small summary cache.

    The cache lives next to the profile journal and is purely informational;
    nothing in the runtime depends on it being present.
    """

    path = _profile_file(paths)
    rows = jsonl.read_all(path)
    by_facet: dict[str, int] = {}
    pinned = 0
    for row in rows:
        if row.get("forgotten"):
            continue
        by_facet[row.get("facet", "?")] = by_facet.get(row.get("facet", "?"), 0) + 1
        if row.get("pinned"):
            pinned += 1
    cache = {
        "rebuilt_at": _now_iso(),
        "facts_total": len([r for r in rows if not r.get("forgotten")]),
        "facts_forgotten": len([r for r in rows if r.get("forgotten")]),
        "by_facet": by_facet,
        "pinned": pinned,
    }
    cache_path = paths.memory / "operator_profile.cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return cache


def stats(paths) -> dict[str, Any]:
    rows = jsonl.read_all(_profile_file(paths))
    by_facet: dict[str, int] = {}
    by_scope: dict[str, int] = {}
    pinned = 0
    forgotten = 0
    for row in rows:
        if row.get("forgotten"):
            forgotten += 1
            continue
        by_facet[row.get("facet", "?")] = by_facet.get(row.get("facet", "?"), 0) + 1
        scope = row.get("scope", "global")
        by_scope[scope] = by_scope.get(scope, 0) + 1
        if row.get("pinned"):
            pinned += 1
    return {
        "total": len([r for r in rows if not r.get("forgotten")]),
        "forgotten": forgotten,
        "pinned": pinned,
        "by_facet": by_facet,
        "by_scope": by_scope,
    }
