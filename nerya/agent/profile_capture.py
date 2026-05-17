"""Operator profile self-learning capture loop.

Observes each agent turn and proposes preference facts after a stable
pattern is seen ``N`` times. The closed-loop counterpart to the manual
``POST /memory/profile/set`` route: the agent now *proposes* facts the
operator can pin / forget, instead of waiting for the operator to enter
them by hand.

Safety rules:

- All proposals respect the same ``_FORBIDDEN_KEYS`` boundary as
  :func:`nerya.agent.operator_profile.set_fact`. Live trading toggles,
  risk limits, approval policy, and vault refs are NEVER touchable by
  this loop, even if the heuristics misfire.
- Proposals are written with ``source="agent_inferred"`` and
  ``pinned=False`` so they show up as suggestions in the dashboard. The
  operator must pin them explicitly to mark them as confirmed.
- The capture state (rolling counter of observations) is persisted at
  ``workspace/memory/profile_capture_state.json`` so the loop survives
  restarts and the threshold is genuinely stable.
- Failure inside any observer is swallowed; the calling turn loop must
  never crash because of a profile-capture bug.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import operator_profile


_LOG = logging.getLogger(__name__)


# How many same-value observations are required before a fact is
# materialized. Kept conservative so we don't pollute the profile.
_THRESHOLD_OCCURRENCES = 3

# Maximum number of distinct candidate (facet,key,value) tuples kept in
# the state file. Older entries are evicted FIFO.
_MAX_STATE_ENTRIES = 64

# Tickers we recognise via cheap regex. Extend in the heuristics block
# only after the matching set_fact path is exercised by a test.
_TICKER_RE = re.compile(r"\b([A-Z]{2,6}(?:USDT|USD|BTC|ETH)?)\b")
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
_TONE_TERSE_HINTS = (
    "concise", "短一点", "简洁", "简短", "be brief", "shorter",
    "less verbose",
)
_TONE_VERBOSE_HINTS = (
    "verbose", "详细", "更详细", "explain more", "deeper", "expand",
)


def _flag_enabled(client: Any) -> bool:
    try:
        from ..runtime import feature_flags as ff
        return bool(ff.is_enabled(client, "runtime.operator_profile"))
    except Exception:  # pragma: no cover - defensive
        return True


def _state_path(client: Any) -> Path:
    return client.config.paths.memory / "profile_capture_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_state(client: Any) -> dict[str, Any]:
    path = _state_path(client)
    if not path.exists():
        return {"candidates": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {"candidates": {}}
    except Exception:
        return {"candidates": {}}


def _save_state(client: Any, state: dict[str, Any]) -> None:
    path = _state_path(client)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _existing_fact_for(
    client: Any, *, facet: str, key: str, value: Any
) -> Optional[dict[str, Any]]:
    """Return an existing non-forgotten fact matching facet+key+value, or None."""
    try:
        rows = operator_profile.list_facts(client.config.paths, facet=facet)
    except Exception:
        return None
    for r in rows:
        if (
            r.get("key") == key
            and r.get("value") == value
            and not r.get("forgotten")
        ):
            return r
    return None


def _candidate_id(facet: str, key: str, value: Any) -> str:
    return f"{facet}::{key}::{value!r}"


def _bump_candidate(
    client: Any,
    *,
    facet: str,
    key: str,
    value: Any,
) -> int:
    """Increment a candidate's observation counter; return the new count."""

    state = _load_state(client)
    candidates: dict[str, Any] = state.setdefault("candidates", {})
    cid = _candidate_id(facet, key, value)
    entry = candidates.get(cid) or {
        "facet": facet,
        "key": key,
        "value": value,
        "count": 0,
        "first_seen": _now_iso(),
        "last_seen": _now_iso(),
        "materialized": False,
    }
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["last_seen"] = _now_iso()
    candidates[cid] = entry

    # FIFO evict if we exceeded the cap. Sort by last_seen ascending.
    if len(candidates) > _MAX_STATE_ENTRIES:
        ordered = sorted(
            candidates.items(), key=lambda kv: kv[1].get("last_seen", "")
        )
        for old_cid, _ in ordered[: len(candidates) - _MAX_STATE_ENTRIES]:
            candidates.pop(old_cid, None)

    _save_state(client, state)
    return int(entry["count"])


def _maybe_propose(
    client: Any,
    *,
    facet: str,
    key: str,
    value: Any,
) -> Optional[dict[str, Any]]:
    """Materialize a proposed fact when the threshold is reached.

    Respects the operator-profile safety boundary (`set_fact` raises on
    forbidden keys; we swallow that and return None).
    """

    # Bail out before counting if a matching fact already exists.
    if _existing_fact_for(client, facet=facet, key=key, value=value) is not None:
        return None

    count = _bump_candidate(client, facet=facet, key=key, value=value)
    if count < _THRESHOLD_OCCURRENCES:
        return None

    # Threshold hit — write the fact and mark the candidate materialized.
    try:
        fact = operator_profile.set_fact(
            client.config.paths,
            facet=facet,
            key=key,
            value=value,
            scope="global",
            pinned=False,
            source="agent_inferred",
        )
    except PermissionError:
        # Trading-safety boundary refused. Pop the candidate so we don't
        # bump it forever.
        state = _load_state(client)
        state.get("candidates", {}).pop(_candidate_id(facet, key, value), None)
        _save_state(client, state)
        return None
    except Exception:
        _LOG.exception("profile_capture: set_fact failed")
        return None

    state = _load_state(client)
    entry = state.get("candidates", {}).get(_candidate_id(facet, key, value))
    if entry:
        entry["materialized"] = True
        entry["fact_id"] = fact.get("id")
        _save_state(client, state)
    return fact


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------


def _detect_language(text: str) -> Optional[str]:
    """Return a coarse language code ('zh' or 'en') or None when unclear."""
    text = (text or "").strip()
    if not text:
        return None
    has_chinese = bool(_CHINESE_RE.search(text))
    if has_chinese and len(_CHINESE_RE.findall(text)) >= 3:
        return "zh"
    # Only call it English if the text is non-trivial and has no CJK
    if not has_chinese and len(re.findall(r"[A-Za-z]", text)) >= 12:
        return "en"
    return None


def _detect_tone(text: str) -> Optional[str]:
    s = (text or "").lower()
    if any(hint in s for hint in _TONE_TERSE_HINTS):
        return "concise"
    if any(hint in s for hint in _TONE_VERBOSE_HINTS):
        return "verbose"
    return None


def _detect_symbols(text: str) -> list[str]:
    """Return a list of likely trading symbols mentioned in ``text``.

    Filters out common English words (e.g., "AND", "FOR") so we don't
    accidentally propose them as preferred universe.
    """
    matches = _TICKER_RE.findall(text or "")
    out: list[str] = []
    seen: set[str] = set()
    # Lightweight stop-set; lowercased matches are skipped above by the
    # uppercase regex but some 2-3 letter ALL-CAPS words slip through.
    stops = {
        "I", "AM", "OK", "USD", "EUR", "GBP", "JPY", "API", "URL", "JSON",
        "PRO", "OK", "NEW", "OLD", "TOP", "BUY", "SELL", "BUYS", "ASK", "BID",
    }
    for raw in matches:
        sym = raw.upper()
        if sym in stops or sym in seen:
            continue
        # require at least 3 chars or end with USDT/USD/BTC/ETH
        if len(sym) < 3 and not sym.endswith(("USDT", "USD", "BTC", "ETH")):
            continue
        seen.add(sym)
        out.append(sym)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def observe_turn(
    client: Any,
    *,
    user_text: str = "",
    reply_text: str = "",
    channel: str = "chat",
) -> dict[str, Any]:
    """Inspect a single turn and possibly propose profile facts.

    Returns a small audit dict the caller can attach to its response
    so the dashboard can show "1 fact proposed this turn".
    """

    if not _flag_enabled(client):
        return {"flag_enabled": False, "proposed": []}

    proposed: list[dict[str, Any]] = []
    try:
        lang = _detect_language(user_text)
        if lang:
            fact = _maybe_propose(
                client,
                facet="style",
                key="preferred_language",
                value=lang,
            )
            if fact is not None:
                proposed.append(fact)

        tone = _detect_tone(user_text)
        if tone:
            fact = _maybe_propose(
                client,
                facet="style",
                key="tone",
                value=tone,
            )
            if fact is not None:
                proposed.append(fact)

        for sym in _detect_symbols(user_text)[:4]:
            fact = _maybe_propose(
                client,
                facet="universe",
                key="symbol",
                value=sym,
            )
            if fact is not None:
                proposed.append(fact)

        # Channel preference — only when we see the same non-default
        # channel multiple times. Very low-signal so we still gate it.
        if channel and channel.strip().lower() not in ("", "chat", "unknown"):
            fact = _maybe_propose(
                client,
                facet="channel",
                key="preferred_channel",
                value=channel.strip().lower(),
            )
            if fact is not None:
                proposed.append(fact)
    except Exception:
        _LOG.exception("profile_capture.observe_turn failed")

    return {
        "flag_enabled": True,
        "proposed": proposed,
        "proposed_count": len(proposed),
    }


__all__ = ["observe_turn"]
