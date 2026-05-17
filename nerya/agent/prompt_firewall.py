"""Prompt firewall — agent-layer entry point for prompt guard checks.

Re-exports the legacy ``wrap_untrusted`` / ``flag_suspicious`` binary helpers
so existing call sites keep working, and adds :func:`classify_user_input` —
the auto-classify helper the agent run-turn entry point and the LLM gateway
should call on every untrusted operator/user/channel input.

The helper:

1. Respects the ``runtime.prompt_guard_review_queue`` feature flag.
2. Calls :func:`prompt_injection.classify` for the three-tier verdict
   (``allow`` | ``review`` | ``block``).
3. Auto-enqueues ``review`` and ``block`` verdicts into the prompt-guard
   queue so the Action Inbox renders them automatically.
4. Never stores the raw prompt; only a sanitized excerpt + content hash
   reaches disk.

Returns a small dict the caller can use to decide whether to proceed:

    {
      "verdict": "allow" | "review" | "block",
      "policy":  "prompt_guard.{allow|review|block}_v1",
      "hits":    [pattern, ...],
      "queue_id": "pg_..." | None,   # set when verdict != "allow" and the
                                      # queue is enabled
      "enqueued": bool,
      "flag_enabled": bool,
    }
"""

from __future__ import annotations

from typing import Any, Optional

from ..security.prompt_injection import (  # noqa: F401  re-exported
    classify,
    flag_suspicious,
    sanitized_excerpt,
    wrap_untrusted,
)


_FLAG = "runtime.prompt_guard_review_queue"


def classify_user_input(
    client: Any,
    *,
    text: str,
    source_route: str = "",
    source_channel: str = "",
    affected_action: str = "",
) -> dict[str, Any]:
    """Classify untrusted operator/user/channel input and auto-enqueue findings.

    Safe to call on every prompt; when the feature flag is disabled, this
    function still returns the verdict (so callers that want to drop hard
    blocks can do so) but does **not** persist anything.

    The helper is intentionally side-effect-light: enqueue failures are
    swallowed so a broken queue file never blocks an agent turn.
    """

    verdict_blob = classify(text or "")
    verdict = verdict_blob.get("verdict") or "allow"
    out: dict[str, Any] = {
        "verdict": verdict,
        "policy": verdict_blob.get("policy", "prompt_guard.allow_v1"),
        "hits": list(verdict_blob.get("hits") or []),
        "queue_id": None,
        "enqueued": False,
        "flag_enabled": True,
    }

    if verdict == "allow":
        return out

    # Check the feature flag lazily so the binary firewall can keep working
    # even when the review queue is intentionally turned off.
    try:
        from ..runtime import feature_flags as ff
        flag_on = bool(ff.is_enabled(client, _FLAG))
    except Exception:  # pragma: no cover - defensive
        flag_on = True
    out["flag_enabled"] = flag_on

    if not flag_on:
        return out

    try:
        from ..security import prompt_guard_queue as pgq
        excerpt = sanitized_excerpt(text or "")
        rec = pgq.enqueue(
            client,
            verdict=verdict,
            policy=out["policy"],
            matched=out["hits"],
            excerpt=excerpt,
            raw_content=text,
            source_route=source_route,
            source_channel=source_channel,
            affected_action=affected_action,
            recommended_action="approve" if verdict == "review" else "reject",
        )
        out["queue_id"] = rec.get("id")
        out["enqueued"] = True
    except Exception:  # pragma: no cover - defensive
        pass
    return out


def extract_user_text(trigger: dict[str, Any]) -> str:
    """Return the operator-visible text inside a trigger payload.

    Mirrors the resolution order used by :class:`AgentKernel.run_turn` so
    the auto-classifier sees the same string the LLM would see.
    """

    if not isinstance(trigger, dict):
        return ""
    payload = trigger.get("payload") or {}
    if isinstance(payload, dict):
        for key in ("text", "message", "prompt"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                return v
    for key in ("raw", "text"):
        v = trigger.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


__all__ = [
    "wrap_untrusted",
    "flag_suspicious",
    "classify",
    "sanitized_excerpt",
    "classify_user_input",
    "extract_user_text",
]
