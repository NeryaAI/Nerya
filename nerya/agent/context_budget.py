"""Context budget + microcompact + autocompact for long coding sessions.

Plan refs:
- ``docs/coding-agent-and-file-tools-improvement-plan.md`` §4.4
- Mirrors Claude Code's three-layer compaction stack:
  1. ``tool_result_budget``  — bound any single observation;
  2. ``microcompact``        — collapse contiguous low-value tool calls;
  3. ``autocompact``         — when the running window crosses a
                                pressure threshold, fold older blocks
                                into a structured summary while
                                preserving open files / pending edits.

Why
---
Long coding sessions destroy a naive context window. A single
``operator.read_file`` of a 1000-line file is ~12k tokens; ten of
those plus their tool envelopes is half the window. Without
compaction the planner blows past max_input_tokens; with naive
truncation we lose the file the agent is editing.

This module is the brains; the kernel + observation feed are the
hands. The functions are pure (no I/O, no global state) so tests can
exercise the compaction policies directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


__all__ = [
    "TokenEstimator",
    "TurnContextBudget",
    "compact_observation",
    "summarise_old_steps",
    "MicroCompactPolicy",
]


# ---- token estimation -------------------------------------------------------


class TokenEstimator:
    """Cheap heuristic token counter (chars / 4) with overrideable model.

    The kernel can swap in a real tokenizer when one is available
    (e.g. tiktoken for OpenAI), but the heuristic is good enough for
    budget decisions: we only need ~10% accuracy to make
    "compact-or-not" calls.
    """

    def __init__(self, *, chars_per_token: float = 4.0) -> None:
        self.chars_per_token = float(chars_per_token)

    def count(self, text: str | Any) -> int:
        if text is None:
            return 0
        if not isinstance(text, str):
            try:
                text = json.dumps(text, ensure_ascii=False, default=str)
            except Exception:
                text = repr(text)
        if self.chars_per_token <= 0:
            return len(text)
        return max(1, int(len(text) / self.chars_per_token))


# ---- per-turn budget --------------------------------------------------------


@dataclass
class TurnContextBudget:
    """Soft limits the kernel respects when assembling the next prompt.

    ``max_input_tokens`` is the *model* limit (e.g. 200k for Claude
    Opus, 128k for GPT-5). ``soft_target`` is the kernel's preferred
    headroom — when the running prompt crosses it we trigger
    autocompact. ``per_observation_tokens`` bounds individual tool
    results so one giant payload cannot starve the rest of the turn.
    """

    max_input_tokens: int = 180_000
    soft_target: int = 140_000
    per_observation_tokens: int = 6_000
    autocompact_pressure: float = 0.85
    keep_recent_turns: int = 8
    keep_open_file_paths: int = 12

    def autocompact_threshold(self) -> int:
        return int(self.max_input_tokens * self.autocompact_pressure)


# ---- microcompact -----------------------------------------------------------


@dataclass
class MicroCompactPolicy:
    """Rules for collapsing contiguous low-value steps."""

    collapse_kinds: tuple[str, ...] = (
        "list_dir", "list_strategies", "list_scripts", "list_routes",
        "list_schedules", "list_accounts", "search_files",
    )
    min_run_length: int = 3
    keep_last_n: int = 1


def microcompact_steps(
    steps: list[dict[str, Any]],
    *,
    policy: MicroCompactPolicy | None = None,
) -> list[dict[str, Any]]:
    """Collapse contiguous low-value steps into one summary block.

    Operates on a list of step dicts (the same shape the kernel
    journals). Returns a *new* list — the caller decides whether to
    swap it into the running prompt.
    """

    pol = policy or MicroCompactPolicy()
    out: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []

    def _flush() -> None:
        if not run:
            return
        if len(run) < pol.min_run_length:
            out.extend(run)
            run.clear()
            return
        head = run[: -pol.keep_last_n] if pol.keep_last_n > 0 else list(run)
        tail = run[-pol.keep_last_n:] if pol.keep_last_n > 0 else []
        kinds = sorted({str(s.get("action") or s.get("kind") or "") for s in head})
        out.append({
            "kind": "microcompact",
            "collapsed": len(head),
            "actions": kinds,
            "first_seq": head[0].get("seq"),
            "last_seq": head[-1].get("seq"),
            "summary": (
                f"Collapsed {len(head)} consecutive low-value steps "
                f"({', '.join(kinds)}); see journal for full detail."
            ),
        })
        out.extend(tail)
        run.clear()

    for step in steps:
        action = str(step.get("action") or step.get("kind") or "")
        if action in pol.collapse_kinds and step.get("ok", True):
            run.append(step)
        else:
            _flush()
            out.append(step)
    _flush()
    return out


# ---- per-observation truncation --------------------------------------------


def compact_observation(
    payload: Any,
    *,
    estimator: TokenEstimator,
    max_tokens: int,
    keep_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Bound a single observation payload to ``max_tokens`` tokens.

    Returns a dict with the (possibly truncated) payload plus a
    ``_compact`` envelope describing what we did. ``keep_keys`` are
    rendered first verbatim so high-signal fields (``error``,
    ``final_report_excerpt``, …) survive.
    """

    if estimator.count(payload) <= max_tokens:
        return {"payload": payload, "_compact": {"truncated": False}}

    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        kept: list[str] = []
        for key in keep_keys:
            if key in payload:
                out[key] = payload[key]
                kept.append(key)
        for key, val in payload.items():
            if key in out:
                continue
            tentative = dict(out)
            tentative[key] = val
            if estimator.count(tentative) > max_tokens:
                break
            out[key] = val
        if estimator.count(out) > max_tokens:
            text = json.dumps(out, ensure_ascii=False, default=str)
            cap_chars = max_tokens * int(estimator.chars_per_token)
            text = text[: max(64, cap_chars - 64)] + "…"
            out = {"_truncated_text": text}
        return {
            "payload": out,
            "_compact": {
                "truncated": True,
                "kept_keys": kept,
                "approach": "key_priority",
            },
        }

    text = json.dumps(payload, ensure_ascii=False, default=str)
    cap_chars = max_tokens * int(estimator.chars_per_token)
    if len(text) > cap_chars:
        text = text[: max(64, cap_chars - 1)] + "…"
    return {"payload": text, "_compact": {"truncated": True, "approach": "head_truncate"}}


# ---- autocompact (turn folding) --------------------------------------------


def summarise_old_steps(
    steps: Iterable[dict[str, Any]],
    *,
    keep_recent: int,
    estimator: TokenEstimator,
    open_file_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Fold old steps into a single rolling summary.

    ``keep_recent`` is the number of recent steps to leave intact;
    everything older is summarised. ``open_file_paths`` is the list
    of files the agent is currently editing — entries touching those
    files are kept whole because their content drives the next edit.
    """

    items = list(steps)
    if len(items) <= keep_recent:
        return {
            "summary": "",
            "summarised": 0,
            "kept_steps": items,
        }
    head = items[:-keep_recent] if keep_recent else items
    tail = items[-keep_recent:] if keep_recent else []
    kept_for_files: list[dict[str, Any]] = []
    other_head: list[dict[str, Any]] = []
    open_set = {str(p) for p in (open_file_paths or ())}
    for s in head:
        path = str(s.get("path") or s.get("payload_path") or "")
        if path and path in open_set:
            kept_for_files.append(s)
        else:
            other_head.append(s)

    counts: dict[str, int] = {}
    last_msg = ""
    for s in other_head:
        action = str(s.get("action") or s.get("kind") or "step")
        counts[action] = counts.get(action, 0) + 1
        if action == "message.send_message" and s.get("ok"):
            last_msg = str(((s.get("result") or {}).get("text")) or last_msg)

    summary_lines = [
        f"Earlier in this session ({len(other_head)} step(s)):",
    ]
    if counts:
        summary_lines.append("- by action: " + ", ".join(
            f"{k}×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
        ))
    if last_msg:
        snip = last_msg.strip().splitlines()[0][:200]
        summary_lines.append(f"- last assistant message head: {snip}…")
    if kept_for_files:
        paths = sorted({str(s.get("path") or "") for s in kept_for_files if s.get("path")})
        summary_lines.append(
            f"- preserved {len(kept_for_files)} step(s) for open files: "
            + ", ".join(paths[:8])
            + ("…" if len(paths) > 8 else "")
        )
    summary = "\n".join(summary_lines)
    return {
        "summary": summary,
        "summarised": len(other_head),
        "kept_steps": kept_for_files + tail,
        "estimated_tokens": estimator.count(summary),
    }
