"""Read-only context surface handed to strategy scripts.

The script sandbox cannot freely import connectors, LLM providers, or
exchange SDKs — the static analyzer blocks that. But strategy scripts
legitimately need to reach *some* Nerya surfaces (market data, on-chain
price, indicator features) to do real work.

Rather than poking a hole in the static analyzer, the script runner
passes an explicit :class:`ScriptContext` to the entry function if it
accepts a ``ctx`` keyword. The context exposes only skill actions that
are on the ``SCRIPT_ALLOWED_SKILLS`` allowlist. Everything else raises
:class:`PermissionError`.

This keeps the script surface narrow, auditable, and extensible — any
new "strategy script can call X" capability needs one edit here plus
an entry in the allowlist below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


# Whitelist of (skill_id, action_name) pairs a script is allowed to
# call. Anything touching orders / wallets / LLM is deliberately absent
# — those go through the top-level trading / approval surfaces so
# Nerya can gate them.
SCRIPT_ALLOWED_SKILLS: set[tuple[str, str]] = {
    # market data (read-only)
    ("market_data", "get_mark_price"),
    ("market_data", "get_ticker"),
    ("market_data", "get_candles"),
    ("market_data", "summarize_market"),
    ("market_data", "calculate_features"),
    # onchain (read-only, incl. new price oracle)
    ("onchain", "get_onchain_price"),
    ("onchain", "get_token_balance"),
    ("onchain", "get_whale_events"),
    ("onchain", "summarize_onchain_activity"),
    # news / social (read-only)
    ("news_social", "get_recent_news"),
    ("news_social", "get_social_pulse"),
}


@dataclass
class ScriptContext:
    """Handed to the script entry function. Call-through-only."""
    config: Any
    _call_skill: Callable[..., Any]

    def skill_call(self, skill_id: str, action: str,
                   **payload: Any) -> Any:
        """Call a whitelisted skill action. Raises PermissionError if
        the (skill, action) pair is not on :data:`SCRIPT_ALLOWED_SKILLS`.
        """
        if (skill_id, action) not in SCRIPT_ALLOWED_SKILLS:
            raise PermissionError(
                f"script attempted to call non-whitelisted skill "
                f"action {skill_id}.{action}; allowed pairs: "
                f"{sorted(SCRIPT_ALLOWED_SKILLS)}"
            )
        return self._call_skill(
            skill_id=skill_id, action=action, payload=payload,
        )


__all__ = ["ScriptContext", "SCRIPT_ALLOWED_SKILLS"]
