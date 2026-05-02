"""Render operator/model-provided market context for chat sessions.

This module intentionally does not infer market domains from keywords
and does not enforce a hard route. It only renders compact session
metadata that another layer has already written so the model can make
its own judgment with the context visible.
"""

from __future__ import annotations

from typing import Any

from .session import SessionStore


def render_session_market_context_block(context: dict[str, Any] | None) -> str:
    if not isinstance(context, dict) or not context.get("market_domain"):
        return ""
    evidence = [str(x) for x in (context.get("evidence") or []) if str(x).strip()]
    lines = [
        "Session market context (advisory):",
        f"- market_domain: {context.get('market_domain')}",
        f"- asset_class: {context.get('asset_class') or 'unknown'}",
        "- generic follow-up strategy requests should inherit this context unless the user explicitly switches domains",
        "- do not substitute unrelated example markets when this context points elsewhere",
        "- If the market scope is ambiguous, state the assumption or ask a clarification before selecting markets",
        "- This is context guidance for agent judgment, not a keyword router or hard tool gate",
    ]
    if evidence:
        lines.append("- evidence: " + ", ".join(evidence[:6]))
    return "\n".join(lines)


def load_session_market_context(store: SessionStore, session_id: str | None) -> dict[str, Any] | None:
    if not session_id:
        return None
    state = store.load(session_id)
    if state is None:
        return None
    context = state.meta.get("market_context")
    return context if isinstance(context, dict) else None


__all__ = [
    "load_session_market_context",
    "render_session_market_context_block",
]
