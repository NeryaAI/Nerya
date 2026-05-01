"""Anthropic prompt caching — system + 3-message rolling window.

Ported from Hermes' `agent/prompt_caching.py`. Applied automatically by
:class:`nerya.llm.providers.AnthropicAdapter` when the tier config sets
``prompt_cache: true``.

Cost model: Anthropic charges 25% of the normal input token rate for
cached segments *but* 125% for the initial cache write. For repetitive
system prompts + tool descriptions this nets to ~75% savings.
"""

from __future__ import annotations

import copy
from typing import Any


def _apply_cache_marker(msg: dict[str, Any], marker: dict[str, Any],
                         native_anthropic: bool = True) -> None:
    role = msg.get("role", "")
    content = msg.get("content")

    if role == "tool":
        if native_anthropic:
            msg["cache_control"] = marker
        return

    if content is None or content == "":
        msg["cache_control"] = marker
        return

    if isinstance(content, str):
        msg["content"] = [{"type": "text", "text": content,
                             "cache_control": marker}]
        return

    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = marker


def apply_anthropic_cache_control(
    messages: list[dict[str, Any]],
    *,
    cache_ttl: str = "5m",
    native_anthropic: bool = True,
) -> list[dict[str, Any]]:
    """Apply up to 4 cache_control breakpoints (Anthropic max).

    Strategy: system prompt + last 3 non-system messages. Deep-copies
    the input so callers can keep their original list untouched.
    """
    out = copy.deepcopy(messages)
    if not out:
        return out

    marker: dict[str, Any] = {"type": "ephemeral"}
    if cache_ttl == "1h":
        marker["ttl"] = "1h"

    used = 0
    if out[0].get("role") == "system":
        _apply_cache_marker(out[0], marker, native_anthropic=native_anthropic)
        used += 1

    remaining = 4 - used
    non_sys = [i for i in range(len(out)) if out[i].get("role") != "system"]
    for idx in non_sys[-remaining:]:
        _apply_cache_marker(out[idx], marker, native_anthropic=native_anthropic)

    return out


__all__ = ["apply_anthropic_cache_control"]
