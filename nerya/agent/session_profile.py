"""Persistent profile for strategy-bound Agent sessions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..core import jsonl
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from .session import SessionStore


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def strategy_agent_session_id(
    *,
    strategy_id: str,
    session_key: dict[str, Any] | None = None,
    policy: str = "per_strategy_market_timeframe",
) -> str:
    key = canonical_json(
        {
            "policy": policy or "per_strategy_market_timeframe",
            "strategy_id": strategy_id,
            "session_key": dict(session_key or {}),
        }
    )
    return "strat_agent_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _profile_hash(profile: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(profile).encode("utf-8")).hexdigest()[:16]


def ensure_strategy_agent_profile(
    *,
    paths: WorkspacePaths,
    session_id: str,
    strategy_id: str,
    profile: dict[str, Any],
    session_key: dict[str, Any] | None,
    policy: str,
) -> dict[str, Any]:
    store = SessionStore(paths.root)
    state = store.ensure(session_id, strategy_id=strategy_id)
    raw_profile = dict(profile or {})
    record = {
        "kind": "strategy_agent_profile",
        "strategy_id": strategy_id,
        "session_id": session_id,
        "session_key": dict(session_key or {}),
        "policy": policy,
        "profile": raw_profile,
        "profile_hash": _profile_hash(raw_profile),
        "updated_at": now_iso(),
    }
    current = state.meta.get("strategy_agent_profile")
    if not isinstance(current, dict) or current.get("profile_hash") != record["profile_hash"]:
        if isinstance(current, dict) and current.get("created_at"):
            record["created_at"] = current["created_at"]
        else:
            record["created_at"] = record["updated_at"]
        state.meta["strategy_agent_profile"] = record
        store.save(state)
        jsonl.append(paths.journal("agent"), {
            "kind": "session.profile.updated",
            "session_id": session_id,
            "strategy_id": strategy_id,
            "profile_hash": record["profile_hash"],
            "policy": policy,
            "ts": record["updated_at"],
        })
    return state.meta.get("strategy_agent_profile") or record


def load_strategy_agent_profile(paths: WorkspacePaths, session_id: str | None) -> dict[str, Any] | None:
    if not session_id:
        return None
    state = SessionStore(paths.root).load(session_id)
    if state is None:
        return None
    profile = state.meta.get("strategy_agent_profile")
    return profile if isinstance(profile, dict) else None


def render_strategy_agent_profile_block(profile_record: dict[str, Any] | None) -> str:
    if not profile_record:
        return ""
    profile = dict(profile_record.get("profile") or {})
    order_rules = [str(x) for x in (profile.get("order_rules") or [])]
    allowed_tools = [str(x) for x in (profile.get("allowed_tools") or [])]
    risk_limits = dict(profile.get("risk_limits") or {})
    lines = [
        "Strategy Agent Session Profile:",
        f"- strategy_id: {profile_record.get('strategy_id')}",
        f"- policy: {profile_record.get('policy')}",
        f"- profile_hash: {profile_record.get('profile_hash')}",
    ]
    if profile.get("title"):
        lines.append(f"- title: {profile.get('title')}")
    if profile.get("role"):
        lines.append(f"- role: {profile.get('role')}")
    if allowed_tools:
        lines.append("- allowed_tools: " + ", ".join(allowed_tools))
    if risk_limits:
        lines.append("- risk_limits: " + canonical_json(risk_limits))
    if order_rules:
        lines.append("Order rules:")
        lines.extend(f"- {rule}" for rule in order_rules)
    lines.append(
        "For trading, use only the current strategy/session context. "
        "Call risk_check before trade_intent_submit when practical; "
        "trade_intent_submit remains guarded by RiskGate and ApprovalGate."
    )
    return "\n".join(lines)


def render_strategy_context_block(
    paths: WorkspacePaths,
    strategy_id: str | None,
    *,
    max_chars: int = 4000,
) -> str:
    """Render the strategy file context for a strategy-bound session.

    Dashboard-initiated strategy chats bind a ``strategy_id`` without a
    ``strategy_agent_profile`` in the session meta (that profile is only
    written by the strategy agent-task executor). This block gives every
    strategy-bound session the same grounding: the strategy record plus
    the on-disk package files (strategy.yml / config.yml / limits.yml /
    prompts / learnings), clipped to ``max_chars`` so a large package
    cannot crowd out the rest of the system prompt.
    """

    sid = str(strategy_id or "").strip()
    if not sid:
        return ""
    try:
        from ..trading import strategy_crud

        detail = strategy_crud.get_detail(paths, sid)
    except Exception:
        return ""

    record = dict(detail.get("strategy") or {})
    lines = [f"Strategy Context (strategy_id={sid}):"]
    for key in (
        "title", "status", "mode", "enabled", "account_id",
        "wallet_id", "markets", "trigger_kinds", "subagents", "path",
    ):
        value = record.get(key)
        if value in (None, "", [], ()):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        lines.append(f"- {key}: {value}")
    for label, key in (
        ("strategy.yml", "strategy_yml"),
        ("config.yml", "config"),
        ("limits.yml", "limits"),
    ):
        data = detail.get(key)
        if isinstance(data, dict) and data:
            lines.append(f"{label}: {canonical_json(data)}")
    prompts = detail.get("prompts")
    if isinstance(prompts, dict) and prompts:
        lines.append("Prompts:")
        for name in sorted(prompts):
            body = str(prompts[name] or "").strip()
            if body:
                lines.append(f"--- {name} ---")
                lines.append(body)
    learnings = str(detail.get("learnings") or "").strip()
    if learnings:
        lines.append("Learnings:")
        lines.append(learnings)

    block = "\n".join(lines)
    if max_chars > 0 and len(block) > max_chars:
        block = block[:max_chars].rstrip() + "\n…[truncated]"
    return block + (
        "\nTreat this strategy's files as the authoritative configuration "
        "for this session; re-read them from the package path with fs "
        "tools (or strategy_view) when you need untruncated detail."
    )


__all__ = [
    "canonical_json",
    "ensure_strategy_agent_profile",
    "load_strategy_agent_profile",
    "render_strategy_agent_profile_block",
    "render_strategy_context_block",
    "strategy_agent_session_id",
]
